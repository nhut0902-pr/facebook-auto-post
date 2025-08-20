# webapp/main.py
from fastapi import FastAPI, Depends, Form
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from .db import SessionLocal, engine
from .models import Base, ScheduledPost, PostedLog, Config
import datetime, os, json

Base.metadata.create_all(bind=engine)
app = FastAPI(title="AutoPost Manager")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/", response_class=HTMLResponse)
def index():
    return "<h3>AutoPost Manager</h3><p>API endpoints: /api/scheduled /api/logs /api/config /api/analytics</p>"

@app.get("/api/scheduled")
def list_scheduled(db: Session = Depends(get_db)):
    items = db.query(ScheduledPost).order_by(ScheduledPost.scheduled_time).all()
    return [{"id": i.id, "title": i.title, "url": i.url, "scheduled_time": i.scheduled_time.isoformat(), "posted": i.posted} for i in items]

@app.post("/api/schedule")
def add_schedule(title: str = Form(...), url: str = Form(...), scheduled_time: str = Form(...), db: Session = Depends(get_db)):
    dt = datetime.datetime.fromisoformat(scheduled_time)
    sp = ScheduledPost(title=title, url=url, scheduled_time=dt)
    db.add(sp); db.commit(); db.refresh(sp)
    return {"ok": True, "id": sp.id}

@app.get("/api/logs")
def get_logs(db: Session = Depends(get_db)):
    items = db.query(PostedLog).order_by(PostedLog.created_at.desc()).limit(200).all()
    return [{"id": i.id, "title": i.title, "url": i.url, "post_id": i.post_id, "status": i.status, "created_at": i.created_at.isoformat()} for i in items]

@app.get("/api/analytics")
def analytics():
    if os.path.exists("../analytics.json"):
        with open("../analytics.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp.main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
