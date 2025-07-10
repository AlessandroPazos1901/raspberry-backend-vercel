# api/raspberry-data.py - Endpoint para recibir datos de Raspberry Pi
from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime
import uuid
import firebase_admin
from firebase_admin import credentials, db, storage
from urllib.parse import parse_qs
import cgi
from io import BytesIO

# Inicializar Firebase (solo una vez)
if not firebase_admin._apps:
    firebase_config = {
        "type": "service_account",
        "project_id": os.environ.get("FIREBASE_PROJECT_ID"),
        "private_key_id": os.environ.get("FIREBASE_PRIVATE_KEY_ID"),
        "private_key": os.environ.get("FIREBASE_PRIVATE_KEY", "").replace('\\n', '\n'),
        "client_email": os.environ.get("FIREBASE_CLIENT_EMAIL"),
        "client_id": os.environ.get("FIREBASE_CLIENT_ID"),
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": os.environ.get("FIREBASE_CLIENT_CERT_URL"),
        "universe_domain": "googleapis.com"
    }
    
    cred = credentials.Certificate(firebase_config)
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://raspberry-monitor-upc-default-rtdb.firebaseio.com/',
        'storageBucket': os.environ.get("FIREBASE_STORAGE_BUCKET", "raspberry-monitor-upc.appspot.com")
    })

# Referencias de Firebase
realtime_db = db
bucket = storage.bucket()

def upload_image_to_firebase(image_data, content_type, raspberry_id):
    """Subir imagen JPG a Firebase Storage"""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        image_filename = f"detections/{raspberry_id}/{timestamp}_{unique_id}.jpg"
        
        # Subir a Firebase Storage
        blob = bucket.blob(image_filename)
        blob.upload_from_string(image_data, content_type=content_type)
        
        # Hacer la imagen pública
        blob.make_public()
        
        # URL pública
        image_url = blob.public_url
        
        print(f"✅ Imagen subida: {image_url}")
        return image_filename, image_url
        
    except Exception as e:
        print(f"❌ Error subiendo imagen: {e}")
        raise

def parse_multipart_data(environ):
    """Parse multipart form data simple"""
    content_type = environ.get('CONTENT_TYPE', '')
    content_length = int(environ.get('CONTENT_LENGTH', 0))
    
    if 'multipart/form-data' not in content_type:
        raise ValueError("Not multipart/form-data")
    
    boundary = content_type.split('boundary=')[1].encode()
    body = environ['wsgi.input'].read(content_length)
    
    parts = body.split(b'--' + boundary)
    form_data = {}
    files = {}
    
    for part in parts[1:-1]:
        if not part.strip():
            continue
            
        header_end = part.find(b'\r\n\r\n')
        if header_end == -1:
            continue
            
        headers = part[:header_end].decode('utf-8')
        content = part[header_end + 4:]
        
        if 'Content-Disposition: form-data' in headers:
            name_start = headers.find('name="') + 6
            name_end = headers.find('"', name_start)
            field_name = headers[name_start:name_end]
            
            if 'filename=' in headers:
                # Es un archivo (imagen)
                filename_start = headers.find('filename="') + 10
                filename_end = headers.find('"', filename_start)
                filename = headers[filename_start:filename_end]
                
                content_type_line = [line for line in headers.split('\n') if 'Content-Type:' in line]
                file_content_type = content_type_line[0].split('Content-Type: ')[1].strip() if content_type_line else 'image/jpeg'
                
                if content.endswith(b'\r\n'):
                    content = content[:-2]
                
                files[field_name] = {
                    'filename': filename,
                    'content': content,
                    'content_type': file_content_type
                }
            else:
                # Campo de texto
                if content.endswith(b'\r\n'):
                    content = content[:-2]
                form_data[field_name] = content.decode('utf-8')
    
    return form_data, files

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # Headers CORS
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            # Parse datos del formulario
            environ = {
                'REQUEST_METHOD': self.command,
                'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                'CONTENT_LENGTH': self.headers.get('Content-Length', '0'),
                'wsgi.input': self.rfile
            }
            
            form_data, files = parse_multipart_data(environ)
            
            # Extraer campos del Raspberry Pi
            raspberry_id = form_data.get('raspberry_id')
            name = form_data.get('name')
            location = form_data.get('location')
            detection_count = int(form_data.get('detection_count', 0))
            temperature = float(form_data.get('temperature', 0))
            humidity = float(form_data.get('humidity', 0))
            latitude = float(form_data.get('latitude', 0))
            longitude = float(form_data.get('longitude', 0))
            
            # Imagen JPG
            image_file = files.get('image')
            if not image_file:
                raise ValueError("No image file provided")
            
            print(f"📨 Datos recibidos de {raspberry_id}: {detection_count} detecciones")
            
            # Subir imagen a Firebase Storage
            image_filename, image_url = upload_image_to_firebase(
                image_file['content'],
                image_file['content_type'],
                raspberry_id
            )
            
            # Timestamp actual
            current_time = datetime.now().isoformat()
            
            # 1. Actualizar información del dispositivo en Realtime Database
            device_ref = realtime_db.reference(f'raspberry_devices/{raspberry_id}')
            device_data = {
                'raspberry_id': raspberry_id,
                'name': name,
                'location': location,
                'latitude': latitude,
                'longitude': longitude,
                'last_seen': current_time,
                'status': 'online',
                'temperature': temperature,
                'humidity': humidity,
                'total_detections': detection_count,
                'updated_at': current_time
            }
            
            # Verificar si es dispositivo nuevo
            existing_device = device_ref.get()
            if existing_device is None:
                device_data['created_at'] = current_time
                print(f"🆕 Nuevo dispositivo registrado: {raspberry_id}")
            
            # Guardar/actualizar dispositivo
            device_ref.update(device_data)
            
            # 2. Guardar detección individual
            detection_data = {
                'raspberry_id': raspberry_id,
                'timestamp': current_time,
                'detection_count': detection_count,
                'temperature': temperature,
                'humidity': humidity,
                'latitude': latitude,
                'longitude': longitude,
                'image_filename': image_filename,
                'image_url': image_url,
                'location': location,
                'created_at': current_time
            }
            
            # Guardar en detections con ID único
            detections_ref = realtime_db.reference('detections')
            detection_key = detections_ref.push(detection_data).key
            
            # 3. Si hay detecciones, crear alerta
            if detection_count > 0:
                alert_data = {
                    'raspberry_id': raspberry_id,
                    'location': location,
                    'detection_count': detection_count,
                    'timestamp': current_time,
                    'image_url': image_url,
                    'alert_type': 'aedes_detected',
                    'status': 'active'
                }
                
                alerts_ref = realtime_db.reference('alerts')
                alerts_ref.push(alert_data)
                print(f"🚨 Alerta creada para {raspberry_id}: {detection_count} detecciones")
            
            # 4. Actualizar estadísticas globales
            try:
                stats_ref = realtime_db.reference('statistics')
                current_stats = stats_ref.get() or {}
                
                # Calcular nuevas estadísticas
                all_devices = realtime_db.reference('raspberry_devices').get() or {}
                all_detections = realtime_db.reference('detections').get() or {}
                
                total_detections = sum([d.get('detection_count', 0) for d in all_detections.values()])
                active_devices = len([d for d in all_devices.values() if d.get('status') == 'online'])
                
                temperatures = [d.get('temperature') for d in all_detections.values() if d.get('temperature')]
                humidities = [d.get('humidity') for d in all_detections.values() if d.get('humidity')]
                
                new_stats = {
                    'total_detections': total_detections,
                    'active_devices': active_devices,
                    'avg_temperature': sum(temperatures) / len(temperatures) if temperatures else 25.0,
                    'avg_humidity': sum(humidities) / len(humidities) if humidities else 65.0,
                    'last_updated': current_time,
                    'total_devices': len(all_devices)
                }
                
                stats_ref.update(new_stats)
                
            except Exception as e:
                print(f"⚠️ Error actualizando estadísticas: {e}")
            
            # Respuesta exitosa
            response = {
                "status": "success",
                "message": f"Data received from {raspberry_id}",
                "raspberry_id": raspberry_id,
                "detection_count": detection_count,
                "image_url": image_url,
                "timestamp": current_time,
                "detection_key": detection_key
            }
            
            print(f"✅ Datos guardados exitosamente en Firebase Realtime Database")
            self.wfile.write(json.dumps(response).encode())
            
        except Exception as e:
            print(f"❌ Error procesando datos: {str(e)}")
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            error_response = {
                "status": "error",
                "message": str(e)
            }
            
            self.wfile.write(json.dumps(error_response).encode())
    
    def do_OPTIONS(self):
        # Preflight CORS
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()