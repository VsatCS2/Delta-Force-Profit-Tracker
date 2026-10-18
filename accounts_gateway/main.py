"""Railway entry point:  uvicorn main:app --host 0.0.0.0 --port $PORT"""
from gateway_app import Settings, create_app

app = create_app(Settings.from_env())
