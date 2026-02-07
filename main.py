"""
SSE Steel Project Tracker - Main Application
FastAPI backend with shared Neon PostgreSQL database
"""
import os
import json
import csv
import io
import base64
from datetime import datetime, date
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

import sqlalchemy as sa
from sqlalchemy import create_engine, text, func, desc, asc
from sqlalchemy.orm import sessionmaker, Session

from models import (
    Base, Company, Contact, Project, ProjectContact,
    Drawing, DrawingRevision, Assembly, Part,
    ScanEvent, Inspection, Shipment, ShipmentItem,
    PurchaseOrder, POItem, AuditLog,
    Inventory, StockLengthConfig, RFQ, RFQItem,
    Transmittal, RFI, ChangeOrder
)
from xml_parser import parse_tekla_xml, generate_qr_content
from routes_phase2 import router as phase2_router, set_session

# ─── CONFIG ──────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./tracker.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
# Strip channel_binding param that psycopg2 doesn't support
if "channel_binding" in DATABASE_URL:
    import re
    DATABASE_URL = re.sub(r'[&?]channel_binding=[^&]*', '', DATABASE_URL)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

# ─── APP SETUP ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created/verified")
    yield

app = FastAPI(title="SSE Steel Project Tracker", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register Phase 2 routes
set_session(SessionLocal)
app.include_router(phase2_router)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─── PYDANTIC MODELS ────────────────────────────────────

class CompanyCreate(BaseModel):
    name: str
    company_type: str = ""
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    fax: str = ""
    email: str = ""
    website: str = ""
    notes: str = ""

class ContactCreate(BaseModel):
    company_id: int
    name: str
    title: str = ""
    phone: str = ""
    cell: str = ""
    email: str = ""
    is_primary: bool = False

class ProjectCreate(BaseModel):
    project_name: str
    customer_id: Optional[int] = None
    gc_id: Optional[int] = None
    engineer_id: Optional[int] = None
    detailer_id: Optional[int] = None
    painter_id: Optional[int] = None
    galvanizer_id: Optional[int] = None
    erector_id: Optional[int] = None
    finish_type: str = "None"
    po_number: str = ""
    ship_to_address: str = ""
    notes: str = ""
    start_date: Optional[str] = None
    due_date: Optional[str] = None

class ScanCreate(BaseModel):
    assembly_id: int
    station: str
    scanned_by: str = ""
    notes: str = ""

class InspectionCreate(BaseModel):
    assembly_id: int
    project_id: int
    inspection_type: str
    result: str
    inspector: str = ""
    wps_number: str = ""
    welder_id: str = ""
    ndt_method: str = ""
    ndt_report_number: str = ""
    findings: str = ""
    corrective_action: str = ""
    checklist_data: Optional[dict] = None
    notes: str = ""

class ShipmentCreate(BaseModel):
    project_id: int
    trailer_type: str = "Flatbed"
    carrier: str = ""
    driver_name: str = ""
    destination: str = "customer"
    destination_company_id: Optional[int] = None
    truck_number: str = ""
    bill_of_lading: str = ""
    notes: str = ""

class ScanToLoadCreate(BaseModel):
    shipment_id: int
    assembly_id: int
    scanned_by: str = ""

# ═══════════════════════════════════════════════════════════
#  API ROUTES
# ═══════════════════════════════════════════════════════════

# ─── HEALTH ──────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "1.0.0", "app": "SSE Steel Project Tracker"}

# ─── JOB NUMBER GENERATION ───────────────────────────────

@app.get("/api/next-job-number")
def next_job_number():
    db = SessionLocal()
    try:
        year_prefix = datetime.now().strftime("%y")
        # Find highest job number for current year
        result = db.query(Project.job_number).filter(
            Project.job_number.like(f"{year_prefix}-%")
        ).all()
        
        max_num = 999  # Start at 1000
        for (jn,) in result:
            try:
                num = int(jn.split("-")[1])
                if num > max_num:
                    max_num = num
            except (IndexError, ValueError):
                pass
        
        return {"next_job_number": f"{year_prefix}-{max_num + 1}"}
    finally:
        db.close()

# ─── COMPANIES (Address Book) ───────────────────────────

@app.get("/api/companies")
def list_companies(company_type: Optional[str] = None):
    db = SessionLocal()
    try:
        q = db.query(Company)
        if company_type:
            q = q.filter(Company.company_type == company_type)
        companies = q.order_by(Company.name).all()
        return [_company_dict(c) for c in companies]
    finally:
        db.close()

@app.post("/api/companies")
def create_company(data: CompanyCreate):
    db = SessionLocal()
    try:
        c = Company(**data.model_dump())
        db.add(c)
        db.commit()
        db.refresh(c)
        return _company_dict(c)
    finally:
        db.close()

@app.put("/api/companies/{company_id}")
def update_company(company_id: int, data: CompanyCreate):
    db = SessionLocal()
    try:
        c = db.query(Company).get(company_id)
        if not c:
            raise HTTPException(404, "Company not found")
        for k, v in data.model_dump().items():
            setattr(c, k, v)
        db.commit()
        return _company_dict(c)
    finally:
        db.close()

@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int):
    db = SessionLocal()
    try:
        c = db.query(Company).get(company_id)
        if not c:
            raise HTTPException(404)
        db.delete(c)
        db.commit()
        return {"deleted": True}
    finally:
        db.close()

@app.post("/api/companies/import-csv")
async def import_companies_csv(file: UploadFile = File(...)):
    """Import companies from CSV (Tekla export format)."""
    db = SessionLocal()
    try:
        content = await file.read()
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        imported = 0
        for row in reader:
            # Flexible column mapping
            name = row.get("Name", row.get("CompanyName", row.get("Company", "")))
            if not name:
                continue
            existing = db.query(Company).filter(Company.name == name).first()
            if existing:
                continue
            c = Company(
                name=name,
                company_type=row.get("Type", row.get("CompanyType", "")),
                address_line1=row.get("Address1", row.get("Address", "")),
                address_line2=row.get("Address2", ""),
                city=row.get("City", ""),
                state=row.get("State", ""),
                zip_code=row.get("Zip", row.get("ZipCode", "")),
                phone=row.get("Phone", ""),
                fax=row.get("Fax", ""),
                email=row.get("Email", ""),
            )
            db.add(c)
            imported += 1
        db.commit()
        return {"imported": imported}
    finally:
        db.close()

# ─── CONTACTS ────────────────────────────────────────────

@app.get("/api/contacts")
def list_contacts(company_id: Optional[int] = None):
    db = SessionLocal()
    try:
        q = db.query(Contact)
        if company_id:
            q = q.filter(Contact.company_id == company_id)
        return [_contact_dict(c) for c in q.all()]
    finally:
        db.close()

@app.post("/api/contacts")
def create_contact(data: ContactCreate):
    db = SessionLocal()
    try:
        c = Contact(**data.model_dump())
        db.add(c)
        db.commit()
        db.refresh(c)
        return _contact_dict(c)
    finally:
        db.close()

# ─── PROJECTS ────────────────────────────────────────────

@app.get("/api/projects")
def list_projects(status: Optional[str] = None):
    db = SessionLocal()
    try:
        q = db.query(Project)
        if status:
            q = q.filter(Project.status == status)
        projects = q.order_by(desc(Project.created_at)).all()
        result = []
        for p in projects:
            d = _project_dict(p)
            # Add counts
            d['assembly_count'] = db.query(Assembly).filter(Assembly.project_id == p.id).count()
            d['drawing_count'] = db.query(Drawing).filter(Drawing.project_id == p.id).count()
            # Station summary
            station_counts = db.query(
                Assembly.current_station, func.count(Assembly.id)
            ).filter(Assembly.project_id == p.id).group_by(Assembly.current_station).all()
            d['station_summary'] = {s: c for s, c in station_counts}
            # Customer name
            if p.customer_id:
                cust = db.query(Company).get(p.customer_id)
                d['customer_name'] = cust.name if cust else ""
            result.append(d)
        return result
    finally:
        db.close()

@app.get("/api/projects/{project_id}")
def get_project(project_id: int):
    db = SessionLocal()
    try:
        p = db.query(Project).get(project_id)
        if not p:
            raise HTTPException(404, "Project not found")
        d = _project_dict(p)
        d['assembly_count'] = db.query(Assembly).filter(Assembly.project_id == p.id).count()
        d['drawing_count'] = db.query(Drawing).filter(Drawing.project_id == p.id).count()
        if p.customer_id:
            cust = db.query(Company).get(p.customer_id)
            d['customer_name'] = cust.name if cust else ""
        return d
    finally:
        db.close()

@app.post("/api/projects")
def create_project(data: ProjectCreate):
    db = SessionLocal()
    try:
        # Generate job number
        year_prefix = datetime.now().strftime("%y")
        result = db.query(Project.job_number).filter(
            Project.job_number.like(f"{year_prefix}-%")
        ).all()
        max_num = 999
        for (jn,) in result:
            try:
                num = int(jn.split("-")[1])
                if num > max_num:
                    max_num = num
            except:
                pass
        job_number = f"{year_prefix}-{max_num + 1}"
        
        p = Project(
            job_number=job_number,
            project_name=data.project_name,
            customer_id=data.customer_id,
            gc_id=data.gc_id,
            engineer_id=data.engineer_id,
            detailer_id=data.detailer_id,
            painter_id=data.painter_id,
            galvanizer_id=data.galvanizer_id,
            erector_id=data.erector_id,
            finish_type=data.finish_type,
            po_number=data.po_number,
            ship_to_address=data.ship_to_address,
            notes=data.notes,
            start_date=date.fromisoformat(data.start_date) if data.start_date else None,
            due_date=date.fromisoformat(data.due_date) if data.due_date else None,
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        return _project_dict(p)
    finally:
        db.close()

@app.put("/api/projects/{project_id}")
def update_project(project_id: int, data: ProjectCreate):
    db = SessionLocal()
    try:
        p = db.query(Project).get(project_id)
        if not p:
            raise HTTPException(404)
        for k, v in data.model_dump().items():
            if k in ('start_date', 'due_date') and v:
                v = date.fromisoformat(v)
            setattr(p, k, v)
        p.updated_at = datetime.utcnow()
        db.commit()
        return _project_dict(p)
    finally:
        db.close()

# ─── XML IMPORT ──────────────────────────────────────────

@app.post("/api/projects/create-from-xml")
async def create_project_from_xml(
    file: UploadFile = File(...),
    project_name: str = Form(""),
    customer_id: Optional[int] = Form(None),
    finish_type: str = Form("None"),
    start_date: str = Form(""),
    due_date: str = Form(""),
):
    """Create a new project and import XML in one step. Accepts .xml or .zip files."""
    import zipfile
    db = SessionLocal()
    try:
        content = await file.read()
        xml_text = None
        
        # Check if it's a zip file
        if file.filename.lower().endswith('.zip') or content[:4] == b'PK\x03\x04':
            zf = zipfile.ZipFile(io.BytesIO(content))
            # Find the XML file inside the zip
            xml_files = [n for n in zf.namelist() if n.lower().endswith('.xml') and not n.startswith('__MACOSX')]
            if not xml_files:
                raise HTTPException(400, "No XML file found inside zip")
            # Use the first (or largest) XML file
            xml_files.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
            xml_text = zf.read(xml_files[0]).decode("utf-8-sig")
        else:
            xml_text = content.decode("utf-8-sig")
        parsed = parse_tekla_xml(xml_text)
        
        # Use XML project name if none provided
        if not project_name and parsed['project'].get('name'):
            project_name = parsed['project']['name']
        if not project_name:
            project_name = file.filename.replace('.xml', '')
        
        # Generate job number
        year_prefix = datetime.now().strftime("%y")
        result = db.query(Project.job_number).filter(
            Project.job_number.like(f"{year_prefix}-%")
        ).all()
        max_num = 999
        for (jn,) in result:
            try:
                num = int(jn.split("-")[1])
                if num > max_num:
                    max_num = num
            except:
                pass
        job_number = f"{year_prefix}-{max_num + 1}"
        
        # Create project
        p = Project(
            job_number=job_number,
            project_name=project_name,
            customer_id=customer_id if customer_id and customer_id > 0 else None,
            finish_type=finish_type,
            start_date=date.fromisoformat(start_date) if start_date else None,
            due_date=date.fromisoformat(due_date) if due_date else None,
        )
        db.add(p)
        db.flush()
        
        # Now import XML data into the project
        assemblies_imported = 0
        parts_imported = 0
        drawings_imported = 0
        
        for dwg_data in parsed['drawings']:
            dwg = Drawing(
                project_id=p.id,
                drawing_number=dwg_data['number'],
                drawing_title=dwg_data.get('title', ''),
                category=dwg_data.get('category', ''),
                current_revision=dwg_data.get('revision_number', '0'),
                revision_description=dwg_data.get('revision_description', ''),
                date_detailed=date.fromisoformat(dwg_data['date_detailed']) if dwg_data.get('date_detailed') else None,
                date_revised=date.fromisoformat(dwg_data['date_revised']) if dwg_data.get('date_revised') else None,
                model_ref=dwg_data.get('model_ref', ''),
            )
            db.add(dwg)
            drawings_imported += 1
        
        for asm_data in parsed['assemblies']:
            main = asm_data.get('main_member')
            asm = Assembly(
                project_id=p.id,
                assembly_id_tekla=asm_data['assembly_id'],
                model_ref=asm_data.get('model_ref', ''),
                assembly_mark=asm_data['mark'],
                assembly_name=asm_data.get('name', ''),
                assembly_quantity=asm_data['quantity'],
                assembly_length_mm=asm_data.get('length_mm', 0),
                drawing_number=asm_data.get('drawing_number', ''),
                sequence_number=asm_data.get('sequence_number', 0),
                sequence_lot_qty=asm_data.get('sequence_lot_qty', 0),
                finish_type=finish_type,
                current_station="Detailing",
            )
            db.add(asm)
            db.flush()
            asm.qr_code_data = generate_qr_content(asm_data['mark'], job_number, asm.id)
            assemblies_imported += 1
            
            for part_data in asm_data['parts']:
                part = Part(
                    assembly_id=asm.id,
                    part_id_tekla=part_data['part_id'],
                    model_ref=part_data.get('model_ref', ''),
                    part_mark=part_data.get('part_mark', ''),
                    is_main_member=part_data['is_main_member'],
                    quantity=part_data['quantity'],
                    shape=part_data['shape'],
                    dimensions=part_data['dimensions'],
                    grade=part_data['grade'],
                    length_inches=part_data.get('length_inches', 0),
                    length_display=part_data.get('length_display', ''),
                    is_hardware=part_data['is_hardware'],
                    remark=part_data.get('remark', ''),
                    pay_category=part_data.get('pay_category', ''),
                )
                db.add(part)
                parts_imported += 1
        
        db.commit()
        db.refresh(p)
        
        return {
            "success": True,
            "project_id": p.id,
            "job_number": job_number,
            "project_name": project_name,
            "assemblies_imported": assemblies_imported,
            "parts_imported": parts_imported,
            "drawings_imported": drawings_imported,
            "summary": parsed['summary']
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Import error: {str(e)}")
    finally:
        db.close()

@app.post("/api/projects/{project_id}/import-xml")
async def import_xml(project_id: int, file: UploadFile = File(...)):
    """Import Tekla PowerFab XML into a project. Accepts .xml or .zip files."""
    import zipfile
    db = SessionLocal()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        
        content = await file.read()
        xml_text = None
        
        # Check if it's a zip file
        if file.filename.lower().endswith('.zip') or content[:4] == b'PK\x03\x04':
            zf = zipfile.ZipFile(io.BytesIO(content))
            xml_files = [n for n in zf.namelist() if n.lower().endswith('.xml') and not n.startswith('__MACOSX')]
            if not xml_files:
                raise HTTPException(400, "No XML file found inside zip")
            xml_files.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
            xml_text = zf.read(xml_files[0]).decode("utf-8-sig")
        else:
            xml_text = content.decode("utf-8-sig")
        
        parsed = parse_tekla_xml(xml_text)
        
        assemblies_imported = 0
        parts_imported = 0
        drawings_imported = 0
        
        # Import drawings
        for dwg_data in parsed['drawings']:
            existing = db.query(Drawing).filter(
                Drawing.project_id == project_id,
                Drawing.drawing_number == dwg_data['number']
            ).first()
            
            if existing:
                # Update revision if newer
                if dwg_data.get('revision_number', '0') > (existing.current_revision or '0'):
                    # Archive old revision
                    rev = DrawingRevision(
                        drawing_id=existing.id,
                        revision_number=existing.current_revision,
                        revision_description=existing.revision_description,
                        date_revised=existing.date_revised,
                    )
                    db.add(rev)
                    existing.current_revision = dwg_data.get('revision_number', '0')
                    existing.revision_description = dwg_data.get('revision_description', '')
                    existing.date_revised = date.fromisoformat(dwg_data['date_revised']) if dwg_data.get('date_revised') else None
            else:
                dwg = Drawing(
                    project_id=project_id,
                    drawing_number=dwg_data['number'],
                    drawing_title=dwg_data.get('title', ''),
                    category=dwg_data.get('category', ''),
                    current_revision=dwg_data.get('revision_number', '0'),
                    revision_description=dwg_data.get('revision_description', ''),
                    date_detailed=date.fromisoformat(dwg_data['date_detailed']) if dwg_data.get('date_detailed') else None,
                    date_revised=date.fromisoformat(dwg_data['date_revised']) if dwg_data.get('date_revised') else None,
                    model_ref=dwg_data.get('model_ref', ''),
                )
                db.add(dwg)
                drawings_imported += 1
        
        # Import assemblies
        for asm_data in parsed['assemblies']:
            existing = db.query(Assembly).filter(
                Assembly.project_id == project_id,
                Assembly.assembly_id_tekla == asm_data['assembly_id']
            ).first()
            
            if existing:
                # Update existing assembly
                existing.assembly_quantity = asm_data['quantity']
                existing.assembly_length_mm = asm_data.get('length_mm', 0)
                existing.sequence_number = asm_data.get('sequence_number', 0)
                continue
            
            # Get main member info for weight estimation
            main = asm_data.get('main_member')
            
            asm = Assembly(
                project_id=project_id,
                assembly_id_tekla=asm_data['assembly_id'],
                model_ref=asm_data.get('model_ref', ''),
                assembly_mark=asm_data['mark'],
                assembly_name=asm_data.get('name', ''),
                assembly_quantity=asm_data['quantity'],
                assembly_length_mm=asm_data.get('length_mm', 0),
                drawing_number=asm_data.get('drawing_number', ''),
                sequence_number=asm_data.get('sequence_number', 0),
                sequence_lot_qty=asm_data.get('sequence_lot_qty', 0),
                finish_type=project.finish_type,
                current_station="Detailing",
            )
            db.add(asm)
            db.flush()  # Get ID
            
            # Generate QR code content
            asm.qr_code_data = generate_qr_content(
                asm_data['mark'], project.job_number, asm.id
            )
            
            assemblies_imported += 1
            
            # Import parts
            for part_data in asm_data['parts']:
                part = Part(
                    assembly_id=asm.id,
                    part_id_tekla=part_data['part_id'],
                    model_ref=part_data.get('model_ref', ''),
                    part_mark=part_data.get('part_mark', ''),
                    is_main_member=part_data['is_main_member'],
                    quantity=part_data['quantity'],
                    shape=part_data['shape'],
                    dimensions=part_data['dimensions'],
                    grade=part_data['grade'],
                    length_inches=part_data.get('length_inches', 0),
                    length_display=part_data.get('length_display', ''),
                    is_hardware=part_data['is_hardware'],
                    remark=part_data.get('remark', ''),
                    pay_category=part_data.get('pay_category', ''),
                )
                db.add(part)
                parts_imported += 1
        
        db.commit()
        
        return {
            "success": True,
            "project_number": parsed['project'].get('number', ''),
            "project_name": parsed['project'].get('name', ''),
            "assemblies_imported": assemblies_imported,
            "parts_imported": parts_imported,
            "drawings_imported": drawings_imported,
            "summary": parsed['summary']
        }
    except ET.ParseError as e:
        raise HTTPException(400, f"Invalid XML: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Import error: {str(e)}")
    finally:
        db.close()

# ─── ASSEMBLIES ──────────────────────────────────────────

@app.get("/api/projects/{project_id}/assemblies")
def list_assemblies(
    project_id: int,
    station: Optional[str] = None,
    search: Optional[str] = None,
    sort: str = "mark"
):
    db = SessionLocal()
    try:
        q = db.query(Assembly).filter(Assembly.project_id == project_id)
        if station:
            q = q.filter(Assembly.current_station == station)
        if search:
            q = q.filter(Assembly.assembly_mark.ilike(f"%{search}%"))
        
        if sort == "mark":
            q = q.order_by(Assembly.assembly_mark)
        elif sort == "station":
            q = q.order_by(Assembly.current_station, Assembly.assembly_mark)
        elif sort == "sequence":
            q = q.order_by(Assembly.sequence_number, Assembly.assembly_mark)
        
        assemblies = q.all()
        result = []
        for a in assemblies:
            d = _assembly_dict(a)
            # Get ALL parts for this assembly
            parts = db.query(Part).filter(Part.assembly_id == a.id).order_by(
                desc(Part.is_main_member), Part.part_mark
            ).all()
            main = None
            d['parts'] = []
            for p in parts:
                if p.is_main_member:
                    main = p
                    d['main_shape'] = p.shape
                    d['main_dimensions'] = p.dimensions
                    d['main_grade'] = p.grade
                    d['main_length'] = p.length_display
                d['parts'].append({
                    'id': p.id,
                    'part_mark': p.part_mark,
                    'is_main': p.is_main_member,
                    'shape': p.shape,
                    'grade': p.grade,
                    'dimensions': p.dimensions,
                    'length_display': p.length_display,
                    'length_inches': p.length_inches,
                    'quantity': p.quantity,
                    'is_hardware': p.is_hardware,
                })
            d['part_count'] = len(parts)
            # Get last scan
            last_scan = db.query(ScanEvent).filter(
                ScanEvent.assembly_id == a.id
            ).order_by(desc(ScanEvent.scanned_at)).first()
            if last_scan:
                d['last_scan'] = {
                    'station': last_scan.station,
                    'by': last_scan.scanned_by,
                    'at': last_scan.scanned_at.isoformat()
                }
            # Get inspection status
            inspections = db.query(Inspection).filter(
                Inspection.assembly_id == a.id
            ).order_by(desc(Inspection.inspection_date)).all()
            d['inspections'] = [{
                'type': i.inspection_type,
                'result': i.result,
                'date': i.inspection_date.isoformat(),
                'inspector': i.inspector
            } for i in inspections]
            
            result.append(d)
        return result
    finally:
        db.close()

@app.get("/api/projects/{project_id}/assembly-summary")
def assembly_summary(project_id: int):
    """Get station counts and progress for a project."""
    db = SessionLocal()
    try:
        station_counts = db.query(
            Assembly.current_station, func.count(Assembly.id)
        ).filter(Assembly.project_id == project_id).group_by(Assembly.current_station).all()
        
        total = db.query(Assembly).filter(Assembly.project_id == project_id).count()
        shipped = db.query(Assembly).filter(
            Assembly.project_id == project_id,
            Assembly.current_station.in_(["Shipped", "Shipped from Galvanizer"])
        ).count()
        
        return {
            "total_assemblies": total,
            "shipped": shipped,
            "percent_complete": round((shipped / total * 100) if total > 0 else 0, 1),
            "by_station": {s: c for s, c in station_counts}
        }
    finally:
        db.close()

# ─── SCAN / STATION TRACKING ────────────────────────────

@app.post("/api/scan")
def scan_barcode(data: ScanCreate):
    """Record a barcode scan event and update assembly station."""
    db = SessionLocal()
    try:
        assembly = db.query(Assembly).get(data.assembly_id)
        if not assembly:
            raise HTTPException(404, "Assembly not found")
        
        # Create scan event
        scan = ScanEvent(
            assembly_id=data.assembly_id,
            station=data.station,
            scanned_by=data.scanned_by,
            notes=data.notes,
        )
        db.add(scan)
        
        # Update assembly current station
        assembly.current_station = data.station
        assembly.updated_at = datetime.utcnow()
        
        # Audit log
        log = AuditLog(
            table_name="tracker_assemblies",
            record_id=assembly.id,
            action="UPDATE",
            field_name="current_station",
            old_value=assembly.current_station,
            new_value=data.station,
            user=data.scanned_by,
        )
        db.add(log)
        
        db.commit()
        
        return {
            "success": True,
            "assembly_mark": assembly.assembly_mark,
            "station": data.station,
            "scanned_at": scan.scanned_at.isoformat(),
        }
    finally:
        db.close()

@app.post("/api/scan/lookup")
def scan_lookup(qr_data: str = Form(...)):
    """Look up assembly by QR code content."""
    db = SessionLocal()
    try:
        # QR format: SSE|JOB_NUM|MARK|DB_ID
        parts = qr_data.split("|")
        if len(parts) >= 4 and parts[0] == "SSE":
            assembly_id = int(parts[3])
            assembly = db.query(Assembly).get(assembly_id)
        else:
            # Try matching by mark
            assembly = db.query(Assembly).filter(
                Assembly.assembly_mark == qr_data
            ).first()
        
        if not assembly:
            raise HTTPException(404, "Assembly not found")
        
        project = db.query(Project).get(assembly.project_id)
        main = db.query(Part).filter(
            Part.assembly_id == assembly.id, Part.is_main_member == True
        ).first()
        
        # Scan history
        scans = db.query(ScanEvent).filter(
            ScanEvent.assembly_id == assembly.id
        ).order_by(desc(ScanEvent.scanned_at)).limit(20).all()
        
        return {
            "assembly": _assembly_dict(assembly),
            "project": {
                "job_number": project.job_number if project else "",
                "name": project.project_name if project else "",
            },
            "main_member": {
                "shape": main.shape,
                "dimensions": main.dimensions,
                "grade": main.grade,
                "length": main.length_display,
            } if main else None,
            "scan_history": [{
                "station": s.station,
                "by": s.scanned_by,
                "at": s.scanned_at.isoformat(),
                "notes": s.notes,
            } for s in scans]
        }
    finally:
        db.close()

@app.get("/api/assemblies/{assembly_id}/history")
def assembly_history(assembly_id: int):
    db = SessionLocal()
    try:
        scans = db.query(ScanEvent).filter(
            ScanEvent.assembly_id == assembly_id
        ).order_by(asc(ScanEvent.scanned_at)).all()
        return [{
            "id": s.id,
            "station": s.station,
            "scanned_by": s.scanned_by,
            "scanned_at": s.scanned_at.isoformat(),
            "notes": s.notes,
        } for s in scans]
    finally:
        db.close()

# ─── QC INSPECTIONS (AISC) ──────────────────────────────

@app.post("/api/inspections")
def create_inspection(data: InspectionCreate):
    db = SessionLocal()
    try:
        insp = Inspection(
            assembly_id=data.assembly_id,
            project_id=data.project_id,
            inspection_type=data.inspection_type,
            result=data.result,
            inspector=data.inspector,
            wps_number=data.wps_number,
            welder_id=data.welder_id,
            ndt_method=data.ndt_method,
            ndt_report_number=data.ndt_report_number,
            findings=data.findings,
            corrective_action=data.corrective_action,
            checklist_data=data.checklist_data,
            notes=data.notes,
        )
        db.add(insp)
        
        # If inspection passes, auto-advance station
        assembly = db.query(Assembly).get(data.assembly_id)
        if assembly and data.result == "Pass":
            if data.inspection_type == "Fit-Up Inspection":
                assembly.current_station = "Weld"
            elif data.inspection_type == "Visual Weld Inspection":
                assembly.current_station = "Finish"
            elif data.inspection_type == "Final Inspection":
                assembly.current_station = "Ready to Ship"
        elif assembly and data.result in ("Fail", "Rework Required"):
            insp.retest_required = True
        
        db.commit()
        db.refresh(insp)
        return {"id": insp.id, "success": True}
    finally:
        db.close()

@app.get("/api/projects/{project_id}/inspections")
def list_inspections(project_id: int, inspection_type: Optional[str] = None):
    db = SessionLocal()
    try:
        q = db.query(Inspection).filter(Inspection.project_id == project_id)
        if inspection_type:
            q = q.filter(Inspection.inspection_type == inspection_type)
        inspections = q.order_by(desc(Inspection.inspection_date)).all()
        result = []
        for i in inspections:
            d = {
                "id": i.id,
                "assembly_id": i.assembly_id,
                "inspection_type": i.inspection_type,
                "result": i.result,
                "inspector": i.inspector,
                "inspection_date": i.inspection_date.isoformat(),
                "wps_number": i.wps_number,
                "welder_id": i.welder_id,
                "ndt_method": i.ndt_method,
                "findings": i.findings,
                "corrective_action": i.corrective_action,
                "retest_required": i.retest_required,
                "notes": i.notes,
            }
            asm = db.query(Assembly).get(i.assembly_id)
            if asm:
                d['assembly_mark'] = asm.assembly_mark
            result.append(d)
        return result
    finally:
        db.close()

# ─── SHIPPING ────────────────────────────────────────────

@app.post("/api/shipments")
def create_shipment(data: ShipmentCreate):
    db = SessionLocal()
    try:
        # Get next load number for project
        max_load = db.query(func.max(Shipment.load_number)).filter(
            Shipment.project_id == data.project_id
        ).scalar() or 0
        
        ship = Shipment(
            project_id=data.project_id,
            load_number=max_load + 1,
            trailer_type=data.trailer_type,
            carrier=data.carrier,
            driver_name=data.driver_name,
            destination=data.destination,
            destination_company_id=data.destination_company_id,
            truck_number=data.truck_number,
            bill_of_lading=data.bill_of_lading,
            notes=data.notes,
        )
        db.add(ship)
        db.commit()
        db.refresh(ship)
        return {"id": ship.id, "load_number": ship.load_number}
    finally:
        db.close()

@app.post("/api/shipments/scan-to-load")
def scan_to_load(data: ScanToLoadCreate):
    """Scan a barcode to add assembly to a shipment load."""
    db = SessionLocal()
    try:
        shipment = db.query(Shipment).get(data.shipment_id)
        if not shipment:
            raise HTTPException(404, "Shipment not found")
        
        assembly = db.query(Assembly).get(data.assembly_id)
        if not assembly:
            raise HTTPException(404, "Assembly not found")
        
        # Check not already on a load
        existing = db.query(ShipmentItem).filter(
            ShipmentItem.assembly_id == data.assembly_id
        ).first()
        if existing:
            raise HTTPException(400, f"Assembly already on Load #{db.query(Shipment).get(existing.shipment_id).load_number}")
        
        item = ShipmentItem(
            shipment_id=data.shipment_id,
            assembly_id=data.assembly_id,
            scanned_by=data.scanned_by,
        )
        db.add(item)
        
        # Update assembly station
        if shipment.destination == "galvanizer":
            assembly.current_station = "Galvanize - Sent Out"
        elif shipment.destination == "painter":
            assembly.current_station = "Paint - Sent Out"
        else:
            assembly.current_station = "Shipped"
        
        # Update shipment totals
        shipment.total_pieces = (shipment.total_pieces or 0) + 1
        if assembly.assembly_weight:
            shipment.total_weight = (shipment.total_weight or 0) + assembly.assembly_weight
        
        db.commit()
        
        return {
            "success": True,
            "assembly_mark": assembly.assembly_mark,
            "load_number": shipment.load_number,
            "total_pieces": shipment.total_pieces,
            "total_weight": shipment.total_weight,
        }
    finally:
        db.close()

@app.get("/api/projects/{project_id}/shipments")
def list_shipments(project_id: int):
    db = SessionLocal()
    try:
        shipments = db.query(Shipment).filter(
            Shipment.project_id == project_id
        ).order_by(desc(Shipment.load_number)).all()
        result = []
        for s in shipments:
            items = db.query(ShipmentItem).filter(ShipmentItem.shipment_id == s.id).all()
            result.append({
                "id": s.id,
                "load_number": s.load_number,
                "trailer_type": s.trailer_type,
                "carrier": s.carrier,
                "destination": s.destination,
                "status": s.status,
                "total_pieces": s.total_pieces,
                "total_weight": s.total_weight,
                "ship_date": s.ship_date.isoformat() if s.ship_date else None,
                "items": [{"assembly_id": i.assembly_id, "scanned_by": i.scanned_by} for i in items]
            })
        return result
    finally:
        db.close()

@app.put("/api/shipments/{shipment_id}/ship")
def mark_shipped(shipment_id: int):
    db = SessionLocal()
    try:
        s = db.query(Shipment).get(shipment_id)
        if not s:
            raise HTTPException(404)
        s.status = "In Transit"
        s.ship_date = datetime.utcnow()
        db.commit()
        return {"success": True}
    finally:
        db.close()

# ─── DRAWINGS ────────────────────────────────────────────

@app.get("/api/projects/{project_id}/drawings")
def list_drawings(project_id: int, category: Optional[str] = None):
    db = SessionLocal()
    try:
        q = db.query(Drawing).filter(Drawing.project_id == project_id)
        if category:
            q = q.filter(Drawing.category == category)
        return [{
            "id": d.id,
            "number": d.drawing_number,
            "title": d.drawing_title,
            "category": d.category,
            "revision": d.current_revision,
            "revision_status": d.revision_status or "IFC",
            "revision_description": d.revision_description,
            "has_pdf": bool(d.pdf_data),
            "date_detailed": d.date_detailed.isoformat() if d.date_detailed else None,
            "date_revised": d.date_revised.isoformat() if d.date_revised else None,
        } for d in q.order_by(Drawing.drawing_number).all()]
    finally:
        db.close()

# ─── QR CODE / LABEL GENERATION ─────────────────────────

@app.get("/api/projects/{project_id}/labels")
def get_labels(project_id: int, marks: Optional[str] = None):
    """Get label data for printing. Pass comma-separated marks or get all."""
    db = SessionLocal()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)
        
        q = db.query(Assembly).filter(Assembly.project_id == project_id)
        if marks:
            mark_list = [m.strip() for m in marks.split(",")]
            q = q.filter(Assembly.assembly_mark.in_(mark_list))
        
        assemblies = q.order_by(Assembly.assembly_mark).all()
        labels = []
        for a in assemblies:
            main = db.query(Part).filter(
                Part.assembly_id == a.id, Part.is_main_member == True
            ).first()
            
            customer = db.query(Company).get(project.customer_id) if project.customer_id else None
            
            labels.append({
                "assembly_id": a.id,
                "qr_data": a.qr_code_data or generate_qr_content(a.assembly_mark, project.job_number, a.id),
                "customer_project": f"{customer.name if customer else ''}-{project.project_name}",
                "mark": a.assembly_mark,
                "job_number": project.job_number,
                "finish": a.finish_type or project.finish_type,
                "sequence": a.sequence_number,
                "quantity": a.assembly_quantity,
                "shape": f"{main.shape} {main.dimensions}" if main else "",
                "length": main.length_display if main else "",
                "weight": a.assembly_weight or 0,
                "part_id": a.id,
                "route": a.route or "",
                "date": datetime.utcnow().strftime("%m/%d/%Y"),
            })
        
        return labels
    finally:
        db.close()

@app.put("/api/assemblies/{assembly_id}/mark-printed")
def mark_label_printed(assembly_id: int):
    db = SessionLocal()
    try:
        a = db.query(Assembly).get(assembly_id)
        if not a:
            raise HTTPException(404)
        a.barcode_printed = True
        a.barcode_print_date = datetime.utcnow()
        db.commit()
        return {"success": True}
    finally:
        db.close()

# ─── MATERIAL / CUT LIST ────────────────────────────────

@app.get("/api/projects/{project_id}/cut-list")
def get_cut_list(project_id: int, shape: Optional[str] = None):
    """Generate cut list from project parts (excluding hardware)."""
    db = SessionLocal()
    try:
        q = db.query(Part).join(Assembly).filter(
            Assembly.project_id == project_id,
            Part.is_hardware == False,
        )
        if shape:
            q = q.filter(Part.shape == shape)
        
        parts = q.order_by(Part.shape, Part.dimensions, Part.length_inches.desc()).all()
        
        # Group by shape + dimensions + grade
        groups = {}
        for p in parts:
            key = f"{p.shape}|{p.dimensions}|{p.grade}"
            if key not in groups:
                groups[key] = {
                    "shape": p.shape,
                    "dimensions": p.dimensions,
                    "grade": p.grade,
                    "items": [],
                    "total_qty": 0,
                    "total_length_ft": 0,
                }
            asm = db.query(Assembly).get(p.assembly_id)
            groups[key]["items"].append({
                "part_mark": p.part_mark,
                "assembly_mark": asm.assembly_mark if asm else "",
                "quantity": p.quantity * (asm.assembly_quantity if asm else 1),
                "length_inches": p.length_inches,
                "length_display": p.length_display,
            })
            qty = p.quantity * (asm.assembly_quantity if asm else 1)
            groups[key]["total_qty"] += qty
            groups[key]["total_length_ft"] += (p.length_inches * qty) / 12
        
        return list(groups.values())
    finally:
        db.close()

# ─── PURCHASE ORDERS ────────────────────────────────────

@app.get("/api/projects/{project_id}/purchase-orders")
def list_pos(project_id: int):
    db = SessionLocal()
    try:
        pos = db.query(PurchaseOrder).filter(
            PurchaseOrder.project_id == project_id
        ).order_by(desc(PurchaseOrder.created_at)).all()
        return [{
            "id": po.id,
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "status": po.status,
            "total_amount": po.total_amount,
            "total_weight": po.total_weight,
            "order_date": po.order_date.isoformat() if po.order_date else None,
        } for po in pos]
    finally:
        db.close()

@app.post("/api/projects/{project_id}/generate-po")
def generate_po_from_cutlist(project_id: int, vendor_id: int = Form(...)):
    """Auto-generate a PO from the project cut list."""
    db = SessionLocal()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)
        
        # Generate PO number
        count = db.query(PurchaseOrder).filter(
            PurchaseOrder.project_id == project_id
        ).count()
        po_number = f"{project.job_number}-PO{count + 1:02d}"
        
        po = PurchaseOrder(
            po_number=po_number,
            project_id=project_id,
            vendor_id=vendor_id,
            order_date=date.today(),
            status="Draft",
        )
        db.add(po)
        db.flush()
        
        # Get cut list grouped by material
        parts = db.query(Part).join(Assembly).filter(
            Assembly.project_id == project_id,
            Part.is_hardware == False,
            Part.is_main_member == True,
        ).all()
        
        total_weight = 0
        for p in parts:
            asm = db.query(Assembly).get(p.assembly_id)
            qty = p.quantity * (asm.assembly_quantity if asm else 1)
            item = POItem(
                po_id=po.id,
                shape=p.shape,
                dimensions=p.dimensions,
                grade=p.grade,
                length_inches=p.length_inches,
                quantity=qty,
            )
            db.add(item)
        
        db.commit()
        return {"po_id": po.id, "po_number": po_number}
    finally:
        db.close()

# ─── PRODUCTION DASHBOARD CROSS-REFERENCE ────────────────
# These endpoints let the existing production dashboard
# pull piece-level status from the tracker

@app.get("/api/production-status/{job_number}")
def production_status_for_dashboard(job_number: str):
    """Cross-reference endpoint for production dashboard integration."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.job_number == job_number).first()
        if not project:
            return {"error": "Project not found"}
        
        assemblies = db.query(Assembly).filter(
            Assembly.project_id == project.id
        ).all()
        
        station_summary = {}
        for a in assemblies:
            station = a.current_station or "Unknown"
            if station not in station_summary:
                station_summary[station] = {"count": 0, "marks": []}
            station_summary[station]["count"] += 1
            station_summary[station]["marks"].append(a.assembly_mark)
        
        total = len(assemblies)
        complete = sum(1 for a in assemblies if a.current_station in (
            "Shipped", "Shipped from Galvanizer", "Delivered"
        ))
        
        return {
            "job_number": job_number,
            "project_name": project.project_name,
            "total_assemblies": total,
            "shipped": complete,
            "percent_complete": round((complete / total * 100) if total > 0 else 0, 1),
            "by_station": station_summary,
        }
    finally:
        db.close()

# ─── HELPER FUNCTIONS ────────────────────────────────────

def _company_dict(c):
    return {
        "id": c.id, "name": c.name, "company_type": c.company_type,
        "address_line1": c.address_line1, "address_line2": c.address_line2,
        "city": c.city, "state": c.state, "zip_code": c.zip_code,
        "phone": c.phone, "fax": c.fax, "email": c.email,
        "website": c.website, "notes": c.notes,
    }

def _contact_dict(c):
    return {
        "id": c.id, "company_id": c.company_id, "name": c.name,
        "title": c.title, "phone": c.phone, "cell": c.cell,
        "email": c.email, "is_primary": c.is_primary,
    }

def _project_dict(p):
    return {
        "id": p.id, "job_number": p.job_number, "project_name": p.project_name,
        "customer_id": p.customer_id, "gc_id": p.gc_id,
        "engineer_id": p.engineer_id, "detailer_id": p.detailer_id,
        "painter_id": p.painter_id, "galvanizer_id": p.galvanizer_id,
        "finish_type": p.finish_type, "contract_weight": p.contract_weight,
        "po_number": p.po_number, "ship_to_address": p.ship_to_address,
        "notes": p.notes, "status": p.status,
        "start_date": p.start_date.isoformat() if p.start_date else None,
        "due_date": p.due_date.isoformat() if p.due_date else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }

def _assembly_dict(a):
    return {
        "id": a.id, "assembly_mark": a.assembly_mark,
        "assembly_name": a.assembly_name, "assembly_quantity": a.assembly_quantity,
        "assembly_length_mm": a.assembly_length_mm,
        "assembly_weight": a.assembly_weight,
        "drawing_number": a.drawing_number,
        "sequence_number": a.sequence_number,
        "finish_type": a.finish_type,
        "current_station": a.current_station,
        "qr_code_data": a.qr_code_data,
        "barcode_printed": a.barcode_printed,
    }

# ─── STATIC FILES ────────────────────────────────────────

# Mount static files LAST
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="assets")

@app.get("/manifest.json")
async def serve_manifest():
    """Serve PWA manifest."""
    manifest_path = os.path.join(static_dir, "manifest.json")
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path, media_type="application/json")
    return JSONResponse({"name": "SSE Tracker", "short_name": "SSE", "display": "standalone"})

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """Serve React SPA for all non-API routes."""
    # Try to serve static file first
    file_path = os.path.join(static_dir, full_path)
    if full_path and os.path.isfile(file_path):
        return FileResponse(file_path)
    index = os.path.join(static_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return HTMLResponse("<h1>SSE Steel Project Tracker</h1><p>Frontend not built yet.</p>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
