from datetime import datetime, timedelta, timezone
from pathlib import Path
import os, hashlib, hmac, secrets
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, String, Text, DateTime, ForeignKey, Table, Column, select, func, inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, Session, sessionmaker
import jwt

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv('LEADFLOW_DATA_DIR', str(BASE / 'data'))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_DB = DATA_DIR / 'leadflow.db'
DB_URL = os.getenv('DATABASE_URL', f"sqlite:///{DEFAULT_DB}")
# Render provides postgres:// or postgresql:// URLs; use the installed psycopg v3 driver.
if DB_URL.startswith('postgres://'):
    DB_URL = 'postgresql+psycopg://' + DB_URL[len('postgres://'):]
elif DB_URL.startswith('postgresql://'):
    DB_URL = 'postgresql+psycopg://' + DB_URL[len('postgresql://'):]
engine = create_engine(DB_URL, connect_args={'check_same_thread': False} if DB_URL.startswith('sqlite') else {}, pool_pre_ping=True)
if DB_URL.startswith('sqlite'):
    with engine.begin() as conn:
        conn.exec_driver_sql('PRAGMA journal_mode=WAL')
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
JWT_SECRET = os.getenv('JWT_SECRET', 'leadflow-development-secret-change-before-production-9f6a2c8d')
if os.getenv('RENDER') and (not os.getenv('JWT_SECRET') or JWT_SECRET.startswith('leadflow-development')):
    raise RuntimeError('Set a strong JWT_SECRET environment variable before deploying to Render.')
JWT_ALG = 'HS256'

class Base(DeclarativeBase): pass

lead_tags = Table('lead_tags', Base.metadata,
    Column('lead_id', ForeignKey('leads.id', ondelete='CASCADE'), primary_key=True),
    Column('tag_id', ForeignKey('tags.id', ondelete='CASCADE'), primary_key=True))

class User(Base):
    __tablename__='users'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    agent_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(80), default='Agent')
    avatar: Mapped[str] = mapped_column(String(8), default='U')
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(default=True)

class Stage(Base):
    __tablename__='stages'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    color: Mapped[str] = mapped_column(String(20))
    position: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)

class Tag(Base):
    __tablename__='tags'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    color: Mapped[str] = mapped_column(String(20))

class Lead(Base):
    __tablename__='leads'
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), default='')
    phone: Mapped[str] = mapped_column(String(50), default='', index=True)
    email: Mapped[str] = mapped_column(String(180), default='', index=True)
    company: Mapped[str] = mapped_column(String(160), default='')
    source: Mapped[str] = mapped_column(String(100), default='')
    notes: Mapped[str] = mapped_column(Text, default='')
    stage_id: Mapped[str] = mapped_column(ForeignKey('stages.id'))
    assigned_user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    tags = relationship('Tag', secondary=lead_tags, lazy='selectin')

class Notification(Base):
    __tablename__='notifications'
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    text: Mapped[str] = mapped_column(Text)
    lead_id: Mapped[Optional[str]] = mapped_column(ForeignKey('leads.id', ondelete='CASCADE'), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    read: Mapped[bool] = mapped_column(default=False)

class Activity(Base):
    __tablename__='activities'
    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    lead_id: Mapped[str] = mapped_column(ForeignKey('leads.id', ondelete='CASCADE'), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(engine)

# Lightweight schema repair for installations created from an earlier prototype.
def ensure_schema():
    insp = inspect(engine)
    if insp.has_table('users'):
        cols = {c['name'] for c in insp.get_columns('users')}
        if 'agent_code' not in cols and DB_URL.startswith('sqlite'):
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE users ADD COLUMN agent_code VARCHAR(40)")
                rows = conn.exec_driver_sql("SELECT id FROM users").fetchall()
                for i, (uid,) in enumerate(rows, 1):
                    conn.exec_driver_sql("UPDATE users SET agent_code=? WHERE id=?", (f'AGT-{i:03d}' if uid != 'admin' else 'ADMIN', uid))
            # Unique index is added only if the legacy database needs it.
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_agent_code ON users(agent_code)")
            except Exception:
                pass
ensure_schema()

def phash(password: str, salt: Optional[str]=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 180000)
    return f'pbkdf2$sha256$180000${salt}${dk.hex()}'

def verify(password, stored):
    try:
        _, alg, it, salt, digest = stored.split('$')
        dk = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception: return False

def token_for(user_id):
    return jwt.encode({'sub': user_id, 'exp': datetime.now(timezone.utc)+timedelta(hours=12)}, JWT_SECRET, algorithm=JWT_ALG)

def get_db():
    db=SessionLocal()
    try: yield db
    finally: db.close()

def current_user(authorization: Optional[str]=Header(None), db: Session=Depends(get_db)):
    if not authorization or not authorization.startswith('Bearer '): raise HTTPException(401,'Authentication required')
    try: uid=jwt.decode(authorization[7:], JWT_SECRET, algorithms=[JWT_ALG])['sub']
    except Exception: raise HTTPException(401,'Invalid or expired token')
    u=db.get(User,uid)
    if not u or not u.active: raise HTTPException(401,'User is inactive')
    return u

def admin_only(u):
    if u.role.lower() not in {'administrator','admin'}: raise HTTPException(403,'Administrator access required')
    return u

def can_access_lead(l, u):
    # Shared CRM workspace: every authenticated user can view and work on every lead.
    return True

def require_lead(l, u):
    if not l or not can_access_lead(l,u): raise HTTPException(404,'Lead not found')
    return l

class Login(BaseModel): email:str; password:str
class UserCreate(BaseModel):
    name:str=Field(min_length=1,max_length=120)
    email:str=Field(min_length=3,max_length=180)
    password:str=Field(min_length=8,max_length=128)
    role:str='Agent'
class UserUpdate(BaseModel):
    email: Optional[str]=None
    password: Optional[str]=Field(default=None,min_length=8,max_length=128)
class LeadIn(BaseModel):
    name:str=''; phone:str=''; email:str=''; company:str=''; source:str=''; stage_id:str; assigned_user_id:Optional[str]=None; tag_ids:list[str]=[]; notes:str=''
class LeadPatch(BaseModel):
    name:Optional[str]=None; phone:Optional[str]=None; email:Optional[str]=None; company:Optional[str]=None; source:Optional[str]=None; stage_id:Optional[str]=None; assigned_user_id:Optional[str]=None; tag_ids:Optional[list[str]]=None; notes:Optional[str]=None
class ActivityIn(BaseModel): text:str=Field(min_length=1,max_length=5000)
class StageIn(BaseModel): name:str; color:str='#64748b'
class TagIn(BaseModel): name:str; color:str='#64748b'
class Reorder(BaseModel): ids:list[str]
class ImportIn(BaseModel): rows:list[dict]
class NotificationRead(BaseModel): ids:list[str]=[]

app=FastAPI(title='LeadFlow CRM API', version='2.0.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=False, allow_methods=['*'], allow_headers=['*'])

@app.get('/api/health')
def health(): return {'ok':True,'service':'LeadFlow CRM','time':datetime.now(timezone.utc).isoformat()}

@app.post('/api/auth/login')
def login(payload:Login, db:Session=Depends(get_db)):
    u=db.scalar(select(User).where(func.lower(User.email)==payload.email.lower().strip()))
    if not u or not verify(payload.password,u.password_hash): raise HTTPException(401,'Invalid email or password')
    return {'token':token_for(u.id),'user':user_out(u)}

@app.get('/api/me')
def me(u=Depends(current_user)): return user_out(u)

@app.get('/api/bootstrap')
def bootstrap(db:Session=Depends(get_db), u=Depends(current_user)):
    users=db.scalars(select(User).where(User.active==True).order_by(User.name)).all()
    stages=db.scalars(select(Stage).where(Stage.active==True).order_by(Stage.position)).all()
    tags=db.scalars(select(Tag).order_by(Tag.name)).all()
    stmt=select(Lead).order_by(Lead.created_at.desc())
    leads=db.scalars(stmt).unique().all()
    return {'users':[user_out(x) for x in users], 'stages':[stage_out(x) for x in stages], 'tags':[tag_out(x) for x in tags], 'leads':[lead_out(x,db,u) for x in leads]}

@app.get('/api/leads/{lead_id}/activities')
def activities(lead_id:str, db:Session=Depends(get_db), u=Depends(current_user)):
    l=require_lead(db.get(Lead,lead_id),u)
    acts=db.scalars(select(Activity).where(Activity.lead_id==l.id).order_by(Activity.created_at.desc())).all()
    return [activity_out(a,db) for a in acts]

@app.post('/api/leads/{lead_id}/activities')
def add_activity(lead_id:str,payload:ActivityIn,db:Session=Depends(get_db),u=Depends(current_user)):
    l=require_lead(db.get(Lead,lead_id),u)
    a=Activity(id='a_'+secrets.token_hex(8),lead_id=l.id,user_id=u.id,text=payload.text)
    l.last_activity_at=datetime.now(timezone.utc); db.add(a); db.commit(); return activity_out(a,db)

@app.post('/api/leads')
def create_lead(p:LeadIn,db:Session=Depends(get_db),u=Depends(current_user)):
    assigned = p.assigned_user_id or u.id
    ensure_refs(db,p.stage_id,assigned,p.tag_ids)
    if not (p.name.strip() or p.phone.strip() or p.email.strip()): raise HTTPException(400,'At least a name, phone, or email is required')
    l=Lead(id='l_'+secrets.token_hex(10),name=p.name.strip(),phone=normalize_phone(p.phone),email=normalize_email(p.email),company=p.company.strip(),source=p.source.strip(),notes=p.notes.strip(),stage_id=p.stage_id,assigned_user_id=assigned)
    l.tags=db.scalars(select(Tag).where(Tag.id.in_(p.tag_ids))).all() if p.tag_ids else []
    db.add(l); db.flush(); db.add(Activity(id='a_'+secrets.token_hex(8),lead_id=l.id,user_id=u.id,text='Lead created'))
    if assigned != u.id:
        db.add(Notification(id='n_'+secrets.token_hex(10),user_id=assigned,lead_id=l.id,text=u.name+' assigned lead “'+l.name+'” to you'))
    db.commit(); db.refresh(l); return lead_out(l,db,u)

@app.patch('/api/leads/{lead_id}')
def patch_lead(lead_id:str,p:LeadPatch,db:Session=Depends(get_db),u=Depends(current_user)):
    l=require_lead(db.get(Lead,lead_id),u)
    data=p.model_dump(exclude_unset=True); tag_ids=data.pop('tag_ids',None)
    if 'stage_id' in data or 'assigned_user_id' in data: ensure_refs(db,data.get('stage_id',l.stage_id),data.get('assigned_user_id',l.assigned_user_id),[])
    old_assignee=l.assigned_user_id
    if 'phone' in data: data['phone']=normalize_phone(data['phone'])
    if 'email' in data: data['email']=normalize_email(data['email'])
    for k,v in data.items(): setattr(l,k,v.strip() if isinstance(v,str) else v)
    if tag_ids is not None: l.tags=db.scalars(select(Tag).where(Tag.id.in_(tag_ids))).all() if tag_ids else []
    l.last_activity_at=datetime.now(timezone.utc)
    if 'stage_id' in data: db.add(Activity(id='a_'+secrets.token_hex(8),lead_id=l.id,user_id=u.id,text='Stage changed'))
    if 'assigned_user_id' in data:
        target=db.get(User,data['assigned_user_id'])
        db.add(Activity(id='a_'+secrets.token_hex(8),lead_id=l.id,user_id=u.id,text='Lead assigned to '+target.name))
        if data['assigned_user_id'] != u.id and data['assigned_user_id'] != old_assignee:
            db.add(Notification(id='n_'+secrets.token_hex(10),user_id=data['assigned_user_id'],lead_id=l.id,text=u.name+' assigned lead “'+l.name+'” to you'))
    db.commit(); db.refresh(l); return lead_out(l,db,u)

@app.post('/api/leads/import')
def import_leads(p:ImportIn, db:Session=Depends(get_db), u=Depends(current_user)):
    stats={'added':0,'updated':0,'skipped':0,'errors':[]}
    try:
        stages={x.name.strip().lower():x for x in db.scalars(select(Stage).where(Stage.active==True)).all() if x.name}
        active_users=db.scalars(select(User).where(User.active==True)).all()
        users={x.name.strip().lower():x for x in active_users if x.name}
        users.update({str(x.agent_code or '').strip().lower():x for x in active_users if str(x.agent_code or '').strip()})
        tags={x.name.strip().lower():x for x in db.scalars(select(Tag)).all() if x.name}
        existing_by_email={normalize_email(x.email):x for x in db.execute(select(Lead)).unique().scalars().all() if normalize_email(x.email)}
        existing_by_phone={normalize_phone(x.phone):x for x in db.execute(select(Lead)).unique().scalars().all() if normalize_phone(x.phone)}
        seen=set()
        default_stage=next(iter(stages.values()),None)
        if not default_stage: raise HTTPException(400,'No active stage exists')
        for i,row in enumerate(p.rows,1):
            try:
                if not isinstance(row,dict): raise ValueError('Spreadsheet row is not an object')
                norm={normalize_key(k):v for k,v in row.items()}
                name=str(norm.get('name','') or '').strip()
                phone=normalize_phone(str(norm.get('phone','') or '').strip())
                email=normalize_email(str(norm.get('email','') or '').strip())
                if not (name or phone or email):
                    stats['skipped']+=1; stats['errors'].append({'row':i,'message':'At least name, phone, or email is required'}); continue
                key=f'e:{email}' if email else f'p:{phone}' if phone else f'n:{name.lower()}'
                if key in seen:
                    stats['skipped']+=1; stats['errors'].append({'row':i,'message':'Duplicate row in this import'}); continue
                seen.add(key)
                existing=(existing_by_email.get(email) if email else None) or (existing_by_phone.get(phone) if phone else None)
                st=stages.get(str(norm.get('stage','') or '').strip().lower()) or default_stage
                owner_value=str(norm.get('assigneduser',norm.get('agentid','')) or '').strip().lower()
                owner=users.get(owner_value) or (u if not owner_value else None)
                tag_names=[x.strip().lower() for x in str(norm.get('tags','') or '').split(',') if x.strip()]
                tag_objs=[tags[x] for x in tag_names if x in tags]
                vals=dict(
                    name=name[:160], phone=phone[:50], email=email[:180],
                    company=str(norm.get('company','') or '').strip()[:160],
                    source=str(norm.get('source','Import') or '').strip()[:100] or 'Import',
                    notes=str(norm.get('notes','') or '').strip(),
                    stage_id=st.id, assigned_user_id=owner.id if owner else u.id
                )
                # Save each row inside a SAVEPOINT so one bad row cannot poison the whole import session.
                with db.begin_nested():
                    if existing:
                        for k,v in vals.items():
                            if v not in ('',None) or k in {'stage_id','assigned_user_id'}: setattr(existing,k,v)
                        if tag_objs: existing.tags=tag_objs
                        existing.last_activity_at=datetime.now(timezone.utc)
                        db.add(Activity(id='a_'+secrets.token_hex(8),lead_id=existing.id,user_id=u.id,text='Updated via import'))
                        stats['updated']+=1
                    else:
                        l=Lead(id='l_'+secrets.token_hex(10),**vals); l.tags=tag_objs
                        db.add(l); db.flush()
                        db.add(Activity(id='a_'+secrets.token_hex(8),lead_id=l.id,user_id=u.id,text='Imported from file'))
                        stats['added']+=1
                        if email: existing_by_email[email]=l
                        if phone: existing_by_phone[phone]=l
            except Exception as exc:
                stats['skipped']+=1
                stats['errors'].append({'row':i,'message':f'{type(exc).__name__}: {exc}'})
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            raise HTTPException(400, f'Import could not be saved: {type(exc).__name__}: {exc}')
        return stats
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(400, f'Import failed: {type(exc).__name__}: {exc}')

@app.get('/api/notifications')
def get_notifications(db:Session=Depends(get_db),u=Depends(current_user)):
    rows=db.scalars(select(Notification).where(Notification.user_id==u.id).order_by(Notification.created_at.desc()).limit(50)).all()
    return [{'id':n.id,'text':n.text,'leadId':n.lead_id,'createdAt':n.created_at.isoformat() if n.created_at else '','read':n.read} for n in rows]

@app.post('/api/notifications/read')
def read_notifications(p:NotificationRead,db:Session=Depends(get_db),u=Depends(current_user)):
    rows=db.scalars(select(Notification).where(Notification.user_id==u.id,Notification.id.in_(p.ids))).all() if p.ids else db.scalars(select(Notification).where(Notification.user_id==u.id,Notification.read==False)).all()
    for n in rows: n.read=True
    db.commit(); return {'ok':True}

@app.get('/api/users')
def get_users(db:Session=Depends(get_db),u=Depends(current_user)):
    return [user_out(x) for x in db.scalars(select(User).order_by(User.name)).all()]

@app.post('/api/users')
def create_user(p:UserCreate, db:Session=Depends(get_db), u=Depends(current_user)):
    admin_only(u)
    name=p.name.strip(); email=normalize_email(p.email); role=p.role.strip() or 'Agent'
    if role.lower() in {'administrator','admin'}:
        raise HTTPException(400,'Use the existing administrator account for admin access')
    if db.scalar(select(User).where(func.lower(User.email)==email)):
        raise HTTPException(409,'An account with this email already exists')
    # Generate the next human-friendly agent ID.
    existing_codes=[x.agent_code for x in db.scalars(select(User)).all() if x.agent_code]
    nums=[]
    for code in existing_codes:
        if code.upper().startswith('AGT-'):
            try: nums.append(int(code.split('-',1)[1]))
            except Exception: pass
    code=f'AGT-{(max(nums)+1 if nums else 1):03d}'
    avatar=''.join([part[0] for part in name.split()[:2]]).upper() or 'AG'
    user=User(id='agent_'+secrets.token_hex(8),agent_code=code,name=name,email=email,role='Agent',avatar=avatar,password_hash=phash(p.password),active=True)
    db.add(user); db.commit(); db.refresh(user)
    return {'user':user_out(user),'credentials':{'agentId':user.agent_code,'email':user.email,'password':p.password}}

@app.delete('/api/users/{user_id}')
def delete_user(user_id:str, db:Session=Depends(get_db), u=Depends(current_user)):
    admin_only(u)
    target=db.get(User,user_id)
    if not target or target.role.lower() in {'administrator','admin'}:
        raise HTTPException(404,'Agent not found')
    admin=db.scalar(select(User).where(User.role.in_(['Administrator','Admin'])))
    if admin:
        db.query(Lead).filter(Lead.assigned_user_id==target.id).update({'assigned_user_id':admin.id})
    db.delete(target); db.commit(); return {'ok':True}
    
@app.patch('/api/users/{user_id}/active')
def set_user_active(user_id:str, active:bool, db:Session=Depends(get_db), u=Depends(current_user)):
    admin_only(u)
    target=db.get(User,user_id)
    if not target or target.role.lower() in {'administrator','admin'}:
        raise HTTPException(404,'Agent not found')
    target.active=active
    db.commit(); return user_out(target)

@app.patch('/api/users/{user_id}')
def update_user(user_id:str, payload:UserUpdate, db:Session=Depends(get_db), u=Depends(current_user)):
    admin_only(u)
    target=db.get(User,user_id)
    if not target or target.role.lower() in {'administrator','admin'}:
        raise HTTPException(404,'Agent not found')
    data=payload.model_dump(exclude_unset=True)
    if 'email' in data:
        email=normalize_email(data['email'])
        if not email:
            raise HTTPException(400,'Email is required')
        other=db.scalar(select(User).where(func.lower(User.email)==email, User.id!=target.id))
        if other:
            raise HTTPException(409,'An account with this email already exists')
        target.email=email
    if 'password' in data and data['password'] is not None:
        if len(data['password']) < 8:
            raise HTTPException(400,'Password must be at least 8 characters')
        target.password_hash=phash(data['password'])
    db.commit(); db.refresh(target)
    return user_out(target)

@app.post('/api/users/{user_id}/reset-password')
def reset_user_password(user_id:str, password:str, db:Session=Depends(get_db), u=Depends(current_user)):
    admin_only(u)
    target=db.get(User,user_id)
    if not target or target.role.lower() in {'administrator','admin'}:
        raise HTTPException(404,'Agent not found')
    if len(password) < 8: raise HTTPException(400,'Password must be at least 8 characters')
    target.password_hash=phash(password); db.commit()
    return {'ok':True,'credentials':{'agentId':target.agent_code,'email':target.email,'password':password}}

@app.post('/api/stages')
def add_stage(p:StageIn,db:Session=Depends(get_db),u=Depends(current_user)):
    mx=db.scalar(select(func.max(Stage.position))) or -1; s=Stage(id='s_'+secrets.token_hex(7),name=p.name.strip(),color=p.color,position=mx+1); db.add(s); db.commit(); return stage_out(s)
@app.patch('/api/stages/{sid}')
def edit_stage(sid:str,p:StageIn,db:Session=Depends(get_db),u=Depends(current_user)):
    s=db.get(Stage,sid)
    if not s: raise HTTPException(404,'Stage not found')
    s.name=p.name.strip(); s.color=p.color; db.commit(); return stage_out(s)
@app.delete('/api/stages/{sid}')
def remove_stage(sid:str,db:Session=Depends(get_db),u=Depends(current_user)):
    s=db.get(Stage,sid)
    if not s: raise HTTPException(404,'Stage not found')
    active=db.scalars(select(Stage).where(Stage.active==True,Stage.id!=sid)).all()
    if not active: raise HTTPException(400,'At least one stage is required')
    replacement=active[0]; db.query(Lead).filter(Lead.stage_id==sid).update({'stage_id':replacement.id}); s.active=False; db.commit(); return {'ok':True}
@app.post('/api/stages/reorder')
def reorder_stages(p:Reorder,db:Session=Depends(get_db),u=Depends(current_user)):
    for i,sid in enumerate(p.ids):
        s=db.get(Stage,sid)
        if s: s.position=i
    db.commit(); return {'ok':True}

@app.post('/api/tags')
def add_tag(p:TagIn,db:Session=Depends(get_db),u=Depends(current_user)):
    t=Tag(id='t_'+secrets.token_hex(7),name=p.name.strip(),color=p.color); db.add(t); db.commit(); return tag_out(t)
@app.patch('/api/tags/{tid}')
def edit_tag(tid:str,p:TagIn,db:Session=Depends(get_db),u=Depends(current_user)):
    t=db.get(Tag,tid)
    if not t: raise HTTPException(404,'Tag not found')
    t.name=p.name.strip(); t.color=p.color; db.commit(); return tag_out(t)
@app.delete('/api/tags/{tid}')
def remove_tag(tid:str,db:Session=Depends(get_db),u=Depends(current_user)):
    t=db.get(Tag,tid)
    if not t: raise HTTPException(404,'Tag not found')
    db.execute(lead_tags.delete().where(lead_tags.c.tag_id==tid)); db.delete(t); db.commit(); return {'ok':True}

@app.delete('/api/leads/{lead_id}')
def delete_lead(lead_id:str,db:Session=Depends(get_db),u=Depends(current_user)):
    l=db.get(Lead,lead_id)
    if not l: raise HTTPException(404,'Lead not found')
    db.delete(l); db.commit(); return {'ok':True}

def normalize_email(value): return str(value or '').strip().lower()
def normalize_phone(value):
    raw=''.join(c for c in str(value or '') if c.isdigit())
    if raw.startswith('0092'): raw=raw[2:]
    if raw.startswith('0') and len(raw)>=10: raw='92'+raw[1:]
    return raw

def normalize_key(value): return ''.join(c for c in str(value or '').lower() if c.isalnum())
def user_out(u): return {'id':u.id,'agentId':u.agent_code,'name':u.name,'email':u.email,'role':u.role,'avatar':u.avatar,'active':u.active}
def stage_out(s): return {'id':s.id,'name':s.name,'color':s.color,'position':s.position,'active':s.active}
def tag_out(t): return {'id':t.id,'name':t.name,'color':t.color}
def activity_out(a,db): return {'id':a.id,'text':a.text,'createdAt':a.created_at.isoformat() if a.created_at else '', 'user':user_out(db.get(User,a.user_id))}
def lead_out(l,db,u):
    acts=db.scalars(select(Activity).where(Activity.lead_id==l.id).order_by(Activity.created_at.desc())).all()
    return {'id':l.id,'name':l.name,'phone':l.phone,'email':l.email,'company':l.company,'source':l.source,'notes':l.notes,'stageId':l.stage_id,'assignedUserId':l.assigned_user_id,'tagIds':[t.id for t in l.tags], 'createdAt':l.created_at.isoformat() if l.created_at else '', 'updatedAt':l.updated_at.isoformat() if l.updated_at else '', 'lastActivityAt':l.last_activity_at.isoformat() if l.last_activity_at else '', 'activities':[activity_out(a,db) for a in acts]}
def ensure_refs(db,stage_id,user_id,tag_ids):
    if not db.get(Stage,stage_id) or not db.get(User,user_id): raise HTTPException(400,'Invalid stage or user')
    if tag_ids and len(db.scalars(select(Tag).where(Tag.id.in_(tag_ids))).all())!=len(set(tag_ids)): raise HTTPException(400,'Invalid tag')

def seed():
    db=SessionLocal()
    # Create only empty CRM configuration/accounts. No demo leads are seeded.
    if db.scalar(select(User).limit(1)):
        db.close(); return
    admin_email=normalize_email(os.getenv('ADMIN_EMAIL','admin@leadflow.local'))
    admin_password=os.getenv('ADMIN_PASSWORD','Admin@123')
    people=[
        ('admin','ADMIN','Administrator',admin_email,'Administrator','AD',admin_password),
    ]
    db.add_all([User(id=i,agent_code=code,name=n,email=e,role=r,avatar=a,password_hash=phash(pw),active=True) for i,code,n,e,r,a,pw in people])
    sts=[('new','New Leads','#6377e8'),('follow','Follow Up','#e6a126'),('second','Second Follow Up','#8a63d9'),('potential','Potential / Pending','#2aa9c5'),('converted','Converted','#27a66f'),('rejected','Rejected','#df5365')]
    db.add_all([Stage(id=i,name=n,color=c,position=p) for p,(i,n,c) in enumerate(sts)])
    tgs=[('hot','Hot','#e5484d'),('vip','VIP','#8b63d2'),('website','Website','#279fbe'),('callback','Callback','#d99423'),('priority','Priority','#3157e8')]
    db.add_all([Tag(id=i,name=n,color=c) for i,n,c in tgs]); db.commit(); db.close()

seed()
frontend = BASE / 'frontend'
app.mount('/', StaticFiles(directory=str(frontend), html=True), name='frontend')
