import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.orm import sessionmaker, declarative_base

# 1. Configuration
# SQL Lite to test
# For MySQL: mysql+pymysql://user:password@localhost/dbname
# For Azure SQL: mssql+pyodbc://user:password@server/dbname...

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")

# 2. Database Setup
connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# 3. Define the ChatMessage Table
class ChatMessageModel(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), index=True) # Cookie Id
    role = Column(String(50), nullable=False)  # 'user' or 'assistant'
    content = Column(Text)
    created_at = Column(DateTime, default=datetime.now)

# 4. Helper to init DB
def init_db():
    Base.metadata.create_all(bind=engine)

# 5. Dependency for simple scripts
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()