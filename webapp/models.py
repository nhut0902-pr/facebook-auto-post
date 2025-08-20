# webapp/models.py
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from .db import Base
import datetime

class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(512))
    url = Column(String(2048))
    image = Column(String(2048), nullable=True)
    scheduled_time = Column(DateTime, default=datetime.datetime.utcnow)
    posted = Column(Boolean, default=False)

class PostedLog(Base):
    __tablename__ = "posted_logs"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(512))
    url = Column(String(2048))
    post_id = Column(String(128))
    status = Column(String(64))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class Config(Base):
    __tablename__ = "config"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True)
    value = Column(Text)
