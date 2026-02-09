"""
SSE Steel Project Tracker - Phase 2 Routes
Nesting, Drawings, Transmittals, RFIs, Change Orders, Inventory
"""
import os
import io
import json
from datetime import datetime, date
from typing import Optional, List

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import func, desc
from sqlalchemy.orm import Session

from models import (
    Project, Drawing, DrawingRevision, Assembly, Part, Company, Contact,
    Inventory, StockLengthConfig, RFQ, RFQItem,
    Transmittal, RFI, ChangeOrder, PurchaseOrder, POItem, DocAttachment
)
from nesting import nest_linear, CutPiece, generate_rfq, DEFAULT_STOCK_LENGTHS

router = APIRouter(prefix="/api")


# ─── SESSION HELPER ──────────────────────────────────────
# Will be set by main.py
SessionLocal = None

def set_session(session_maker):
    global SessionLocal
    SessionLocal = session_maker

def get_db():
    db = SessionLocal()
    try:
        return db
    except:
        db.close()
        raise


# ═══════════════════════════════════════════════════════════
#  DRAWINGS WITH PDF UPLOAD
# ═══════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/drawings/{drawing_id}/upload-pdf")
async def upload_drawing_pdf(project_id: int, drawing_id: int, file: UploadFile = File(...)):
    db = get_db()
    try:
        drawing = db.query(Drawing).filter(Drawing.id == drawing_id, Drawing.project_id == project_id).first()
        if not drawing:
            raise HTTPException(404, "Drawing not found")
        
        import base64
        content = await file.read()
        drawing.pdf_data = base64.b64encode(content).decode('utf-8')
        db.commit()
        return {"success": True, "drawing_id": drawing.id}
    finally:
        db.close()

@router.get("/drawings/{drawing_id}/pdf")
async def get_drawing_pdf(drawing_id: int):
    from fastapi.responses import Response
    db = get_db()
    try:
        drawing = db.query(Drawing).get(drawing_id)
        if not drawing or not drawing.pdf_data:
            raise HTTPException(404, "PDF not found")
        
        import base64
        pdf_bytes = base64.b64decode(drawing.pdf_data)
        filename = f"{drawing.drawing_number}_Rev{drawing.current_revision}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename={filename}"}
        )
    finally:
        db.close()

@router.put("/drawings/{drawing_id}/revision")
async def update_drawing_revision(
    drawing_id: int,
    revision_number: str = Form(...),
    revision_description: str = Form(""),
    revision_status: str = Form("IFC"),
    file: Optional[UploadFile] = File(None),
):
    """Update drawing revision, archive old one."""
    db = get_db()
    try:
        drawing = db.query(Drawing).get(drawing_id)
        if not drawing:
            raise HTTPException(404)
        
        # Archive current revision
        rev = DrawingRevision(
            drawing_id=drawing.id,
            revision_number=drawing.current_revision,
            revision_description=drawing.revision_description,
            date_revised=drawing.date_revised,
            pdf_data=drawing.pdf_data,
        )
        db.add(rev)
        
        # Update to new revision
        drawing.current_revision = revision_number
        drawing.revision_description = revision_description
        drawing.revision_status = revision_status
        drawing.date_revised = date.today()
        
        if file:
            import base64
            content = await file.read()
            drawing.pdf_data = base64.b64encode(content).decode('utf-8')
        
        db.commit()
        return {"success": True, "revision": revision_number}
    finally:
        db.close()

@router.get("/drawings/{drawing_id}/revisions")
def get_drawing_revisions(drawing_id: int):
    db = get_db()
    try:
        revs = db.query(DrawingRevision).filter(
            DrawingRevision.drawing_id == drawing_id
        ).order_by(desc(DrawingRevision.created_at)).all()
        return [{
            "id": r.id,
            "revision_number": r.revision_number,
            "description": r.revision_description,
            "date_revised": r.date_revised.isoformat() if r.date_revised else None,
            "has_pdf": bool(r.pdf_data),
        } for r in revs]
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════
#  NESTING / CUTTING STOCK OPTIMIZATION
# ═══════════════════════════════════════════════════════════

@router.post("/projects/{project_id}/nest")
def run_nesting(project_id: int):
    """Run nesting optimization for a project."""
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)
        
        # Get all non-hardware parts with assemblies
        parts = db.query(Part).join(Assembly).filter(
            Assembly.project_id == project_id,
            Part.is_hardware == False,
            Part.length_inches > 0,
            Part.shape != "PL",  # Skip plates for now
        ).all()
        
        # Build cut pieces list
        cut_pieces = []
        for p in parts:
            asm = db.query(Assembly).get(p.assembly_id)
            cut_pieces.append(CutPiece(
                part_mark=p.part_mark,
                assembly_mark=asm.assembly_mark if asm else "",
                shape=p.shape,
                dimensions=p.dimensions,
                grade=p.grade,
                length_inches=p.length_inches,
                quantity=p.quantity * (asm.assembly_quantity if asm else 1),
                project_id=project_id,
            ))
        
        # Get custom stock lengths if configured
        stock_configs = db.query(StockLengthConfig).filter(
            StockLengthConfig.is_active == True
        ).all()
        
        custom_lengths = {}
        if stock_configs:
            for sc in stock_configs:
                cat = sc.shape_category.upper()
                if cat not in custom_lengths:
                    custom_lengths[cat] = []
                custom_lengths[cat].append(sc.length_feet)
        
        stock_lengths = {**DEFAULT_STOCK_LENGTHS, **custom_lengths}
        
        # Get inventory
        inv_items = db.query(Inventory).filter(
            Inventory.quantity > 0,
            (Inventory.reserved_for_project == None) | (Inventory.reserved_for_project == project_id)
        ).all()
        
        inventory = [{
            "id": inv.id,
            "shape": inv.shape,
            "dimensions": inv.dimensions,
            "grade": inv.grade,
            "length_inches": inv.length_inches,
            "quantity": inv.quantity,
        } for inv in inv_items]
        
        # Run nesting
        result = nest_linear(cut_pieces, stock_lengths, inventory)
        
        # Format response
        bars_data = []
        for bar in result.bars:
            bars_data.append({
                "shape": bar.shape,
                "dimensions": bar.dimensions,
                "grade": bar.grade,
                "stock_length_ft": round(bar.stock_length_inches / 12, 1),
                "from_inventory": bar.from_inventory,
                "utilization": bar.utilization,
                "waste_inches": round(bar.waste_inches, 2),
                "cuts": [{
                    "part_mark": c.part_mark,
                    "assembly_mark": c.assembly_mark,
                    "length_inches": c.length_inches,
                    "length_ft": round(c.length_inches / 12, 2),
                } for c in bar.cuts]
            })
        
        return {
            "success": True,
            "bars": bars_data,
            "summary": result.summary,
            "unplaced": [{
                "part_mark": u.part_mark,
                "shape": u.shape,
                "dimensions": u.dimensions,
                "length_inches": u.length_inches,
            } for u in result.unplaced],
        }
    finally:
        db.close()

@router.post("/projects/{project_id}/nest/create-rfq")
def create_rfq_from_nest(project_id: int):
    """Run nesting and create an RFQ from the purchase list."""
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)
        
        # Run nesting first
        parts = db.query(Part).join(Assembly).filter(
            Assembly.project_id == project_id,
            Part.is_hardware == False,
            Part.length_inches > 0,
            Part.shape != "PL",
        ).all()
        
        cut_pieces = []
        for p in parts:
            asm = db.query(Assembly).get(p.assembly_id)
            cut_pieces.append(CutPiece(
                part_mark=p.part_mark,
                assembly_mark=asm.assembly_mark if asm else "",
                shape=p.shape,
                dimensions=p.dimensions,
                grade=p.grade,
                length_inches=p.length_inches,
                quantity=p.quantity * (asm.assembly_quantity if asm else 1),
                project_id=project_id,
            ))
        
        nest_result = nest_linear(cut_pieces)
        rfq_items = generate_rfq(nest_result, project.project_name)
        
        # Create RFQ record
        count = db.query(RFQ).filter(RFQ.project_id == project_id).count()
        rfq = RFQ(
            rfq_number=f"{project.job_number}-RFQ{count + 1:02d}",
            project_id=project_id,
            status="Draft",
        )
        db.add(rfq)
        db.flush()
        
        for item in rfq_items:
            ri = RFQItem(
                rfq_id=rfq.id,
                shape=item["shape"],
                dimensions=item["dimensions"],
                grade=item["grade"],
                length_feet=item["length_ft"],
                quantity=item["quantity"],
                total_feet=item["total_feet"],
                description=item["description"],
            )
            db.add(ri)
        
        db.commit()
        return {"rfq_id": rfq.id, "rfq_number": rfq.rfq_number, "items": len(rfq_items)}
    finally:
        db.close()


# ─── STOCK LENGTH CONFIG ─────────────────────────────────

@router.get("/stock-lengths")
def list_stock_lengths():
    db = get_db()
    try:
        configs = db.query(StockLengthConfig).order_by(
            StockLengthConfig.shape_category, StockLengthConfig.length_feet
        ).all()
        
        if not configs:
            # Return defaults
            result = []
            for cat, lengths in DEFAULT_STOCK_LENGTHS.items():
                for l in lengths:
                    result.append({"shape_category": cat, "length_feet": l, "is_default": True})
            return result
        
        return [{"id": c.id, "shape_category": c.shape_category, "length_feet": c.length_feet, "is_active": c.is_active, "is_default": False} for c in configs]
    finally:
        db.close()

@router.post("/stock-lengths")
def save_stock_lengths(items: List[dict]):
    db = get_db()
    try:
        # Clear existing
        db.query(StockLengthConfig).delete()
        for item in items:
            sc = StockLengthConfig(
                shape_category=item["shape_category"],
                length_feet=item["length_feet"],
                is_active=item.get("is_active", True),
            )
            db.add(sc)
        db.commit()
        return {"saved": len(items)}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════
#  INVENTORY
# ═══════════════════════════════════════════════════════════

@router.get("/inventory")
def list_inventory(shape: Optional[str] = None, in_stock: bool = True):
    db = get_db()
    try:
        q = db.query(Inventory)
        if shape:
            q = q.filter(Inventory.shape == shape)
        if in_stock:
            q = q.filter(Inventory.quantity > 0)
        items = q.order_by(Inventory.shape, Inventory.dimensions).all()
        return [{
            "id": i.id, "shape": i.shape, "dimensions": i.dimensions, "grade": i.grade,
            "length_inches": i.length_inches, "length_display": i.length_display,
            "quantity": i.quantity, "location": i.location, "heat_number": i.heat_number,
            "reserved_for_project": i.reserved_for_project, "notes": i.notes,
        } for i in items]
    finally:
        db.close()

@router.post("/inventory")
def add_inventory(
    shape: str = Form(...), dimensions: str = Form(...), grade: str = Form(...),
    length_inches: float = Form(0), quantity: int = Form(1),
    location: str = Form(""), heat_number: str = Form(""), notes: str = Form(""),
):
    db = get_db()
    try:
        inv = Inventory(
            shape=shape, dimensions=dimensions, grade=grade,
            length_inches=length_inches, quantity=quantity,
            location=location, heat_number=heat_number, notes=notes,
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        return {"id": inv.id, "success": True}
    finally:
        db.close()

@router.put("/inventory/{inv_id}")
def update_inventory(inv_id: int, quantity: int = Form(...)):
    db = get_db()
    try:
        inv = db.query(Inventory).get(inv_id)
        if not inv:
            raise HTTPException(404)
        inv.quantity = quantity
        db.commit()
        return {"success": True}
    finally:
        db.close()

@router.delete("/inventory/{inv_id}")
def delete_inventory(inv_id: int):
    db = get_db()
    try:
        inv = db.query(Inventory).get(inv_id)
        if inv:
            db.delete(inv)
            db.commit()
        return {"deleted": True}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════
#  RFQ MANAGEMENT
# ═══════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/rfqs")
def list_rfqs(project_id: int):
    db = get_db()
    try:
        rfqs = db.query(RFQ).filter(RFQ.project_id == project_id).order_by(desc(RFQ.created_at)).all()
        result = []
        for r in rfqs:
            items = db.query(RFQItem).filter(RFQItem.rfq_id == r.id).all()
            result.append({
                "id": r.id, "rfq_number": r.rfq_number, "status": r.status,
                "created_at": r.created_at.isoformat(),
                "item_count": len(items),
                "items": [{
                    "id": i.id, "description": i.description, "quantity": i.quantity,
                    "length_feet": i.length_feet, "total_feet": i.total_feet,
                    "quoted_price": i.quoted_price, "selected": i.selected,
                } for i in items]
            })
        return result
    finally:
        db.close()

@router.post("/rfqs/{rfq_id}/convert-to-po")
def convert_rfq_to_po(rfq_id: int, vendor_id: int = Form(...)):
    """Convert selected RFQ items to a Purchase Order."""
    db = get_db()
    try:
        rfq = db.query(RFQ).get(rfq_id)
        if not rfq:
            raise HTTPException(404)
        
        items = db.query(RFQItem).filter(RFQItem.rfq_id == rfq_id).all()
        
        project = db.query(Project).get(rfq.project_id)
        count = db.query(PurchaseOrder).filter(PurchaseOrder.project_id == rfq.project_id).count()
        po = PurchaseOrder(
            po_number=f"{project.job_number}-PO{count + 1:02d}",
            project_id=rfq.project_id,
            vendor_id=vendor_id,
            order_date=date.today(),
            status="Draft",
        )
        db.add(po)
        db.flush()
        
        total = 0
        for item in items:
            poi = POItem(
                po_id=po.id,
                shape=item.shape,
                dimensions=item.dimensions,
                grade=item.grade,
                length_inches=item.length_feet * 12,
                quantity=item.quantity,
                unit_price=item.quoted_price or 0,
                total_price=(item.quoted_price or 0) * item.quantity,
            )
            db.add(poi)
            total += poi.total_price or 0
        
        po.total_amount = total
        rfq.status = "Ordered"
        db.commit()
        return {"po_id": po.id, "po_number": po.po_number}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════
#  TRANSMITTALS
# ═══════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/transmittals")
def list_transmittals(project_id: int):
    db = get_db()
    try:
        items = db.query(Transmittal).filter(
            Transmittal.project_id == project_id
        ).order_by(desc(Transmittal.created_at)).all()
        return [{
            "id": t.id, "transmittal_number": t.transmittal_number,
            "to_contact": t.to_contact, "to_email": t.to_email,
            "subject": t.subject, "action_required": t.action_required,
            "status": t.status, "sent_date": t.sent_date.isoformat() if t.sent_date else None,
            "drawing_numbers": t.drawing_numbers,
            "created_at": t.created_at.isoformat(),
        } for t in items]
    finally:
        db.close()

@router.post("/projects/{project_id}/transmittals")
def create_transmittal(
    project_id: int,
    to_company_id: int = Form(0), to_contact: str = Form(""),
    to_email: str = Form(""), subject: str = Form(""),
    message: str = Form(""), drawing_numbers: str = Form(""),
    action_required: str = Form("For Review"),
):
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)
        count = db.query(Transmittal).filter(Transmittal.project_id == project_id).count()
        t = Transmittal(
            project_id=project_id,
            transmittal_number=f"{project.job_number}-T{count + 1:03d}",
            to_company_id=to_company_id if to_company_id > 0 else None,
            to_contact=to_contact, to_email=to_email,
            subject=subject, message=message,
            drawing_numbers=drawing_numbers,
            action_required=action_required,
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return {"id": t.id, "transmittal_number": t.transmittal_number}
    finally:
        db.close()

@router.put("/transmittals/{transmittal_id}/send")
def mark_transmittal_sent(transmittal_id: int):
    db = get_db()
    try:
        t = db.query(Transmittal).get(transmittal_id)
        if not t:
            raise HTTPException(404)
        t.status = "Sent"
        t.sent_date = datetime.utcnow()
        db.commit()
        return {"success": True}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════
#  RFIs
# ═══════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/rfis")
def list_rfis(project_id: int):
    db = get_db()
    try:
        items = db.query(RFI).filter(RFI.project_id == project_id).order_by(desc(RFI.created_at)).all()
        return [{
            "id": r.id, "rfi_number": r.rfi_number, "subject": r.subject,
            "question": r.question, "response": r.response,
            "submitted_to": r.submitted_to, "to_email": r.to_email,
            "priority": r.priority, "status": r.status,
            "drawing_reference": r.drawing_reference,
            "date_submitted": r.date_submitted.isoformat() if r.date_submitted else None,
            "date_required": r.date_required.isoformat() if r.date_required else None,
            "date_responded": r.date_responded.isoformat() if r.date_responded else None,
            "impact_cost": r.impact_cost, "impact_schedule": r.impact_schedule,
            "notes": r.notes, "created_at": r.created_at.isoformat(),
        } for r in items]
    finally:
        db.close()

@router.post("/projects/{project_id}/rfis")
def create_rfi(
    project_id: int,
    subject: str = Form(...), question: str = Form(...),
    submitted_to: str = Form(""), to_email: str = Form(""),
    to_company_id: int = Form(0),
    drawing_reference: str = Form(""), detail_reference: str = Form(""),
    priority: str = Form("Normal"), date_required: str = Form(""),
    impact_cost: bool = Form(False), impact_schedule: bool = Form(False),
):
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)
        count = db.query(RFI).filter(RFI.project_id == project_id).count()
        rfi = RFI(
            project_id=project_id,
            rfi_number=f"{project.job_number}-RFI{count + 1:03d}",
            subject=subject, question=question,
            submitted_to=submitted_to, to_email=to_email,
            to_company_id=to_company_id if to_company_id > 0 else None,
            drawing_reference=drawing_reference,
            detail_reference=detail_reference,
            priority=priority,
            date_required=date.fromisoformat(date_required) if date_required else None,
            impact_cost=impact_cost, impact_schedule=impact_schedule,
        )
        db.add(rfi)
        db.commit()
        db.refresh(rfi)
        return {"id": rfi.id, "rfi_number": rfi.rfi_number}
    finally:
        db.close()

@router.put("/rfis/{rfi_id}/respond")
def respond_to_rfi(rfi_id: int, response: str = Form(...)):
    db = get_db()
    try:
        rfi = db.query(RFI).get(rfi_id)
        if not rfi:
            raise HTTPException(404)
        rfi.response = response
        rfi.status = "Responded"
        rfi.date_responded = datetime.utcnow()
        db.commit()
        return {"success": True}
    finally:
        db.close()

@router.put("/rfis/{rfi_id}/send")
def send_rfi(rfi_id: int):
    db = get_db()
    try:
        rfi = db.query(RFI).get(rfi_id)
        if not rfi:
            raise HTTPException(404)
        rfi.status = "Sent"
        rfi.date_submitted = datetime.utcnow()
        db.commit()
        return {"success": True}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════
#  CHANGE ORDERS
# ═══════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/change-orders")
def list_change_orders(project_id: int):
    db = get_db()
    try:
        items = db.query(ChangeOrder).filter(
            ChangeOrder.project_id == project_id
        ).order_by(desc(ChangeOrder.created_at)).all()
        return [{
            "id": c.id, "co_number": c.co_number, "title": c.title,
            "description": c.description, "reason": c.reason,
            "cost_impact": c.cost_impact, "schedule_impact_days": c.schedule_impact_days,
            "weight_change_lbs": c.weight_change_lbs,
            "status": c.status, "rfi_reference": c.rfi_reference,
            "drawing_references": c.drawing_references,
            "submitted_date": c.submitted_date.isoformat() if c.submitted_date else None,
            "approved_date": c.approved_date.isoformat() if c.approved_date else None,
            "created_at": c.created_at.isoformat(),
        } for c in items]
    finally:
        db.close()

@router.post("/projects/{project_id}/change-orders")
def create_change_order(
    project_id: int,
    title: str = Form(...), description: str = Form(""),
    reason: str = Form("Design Change"),
    drawing_references: str = Form(""), rfi_reference: str = Form(""),
    cost_impact: float = Form(0), schedule_impact_days: int = Form(0),
    weight_change_lbs: float = Form(0),
):
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)
        count = db.query(ChangeOrder).filter(ChangeOrder.project_id == project_id).count()
        co = ChangeOrder(
            project_id=project_id,
            co_number=f"{project.job_number}-CO{count + 1:03d}",
            title=title, description=description, reason=reason,
            drawing_references=drawing_references,
            rfi_reference=rfi_reference,
            cost_impact=cost_impact,
            schedule_impact_days=schedule_impact_days,
            weight_change_lbs=weight_change_lbs,
        )
        db.add(co)
        db.commit()
        db.refresh(co)
        return {"id": co.id, "co_number": co.co_number}
    finally:
        db.close()

@router.put("/change-orders/{co_id}/submit")
def submit_change_order(co_id: int):
    db = get_db()
    try:
        co = db.query(ChangeOrder).get(co_id)
        if not co:
            raise HTTPException(404)
        co.status = "Submitted"
        co.submitted_date = date.today()
        db.commit()
        return {"success": True}
    finally:
        db.close()

@router.put("/change-orders/{co_id}/approve")
def approve_change_order(co_id: int, approved_by: str = Form("")):
    db = get_db()
    try:
        co = db.query(ChangeOrder).get(co_id)
        if not co:
            raise HTTPException(404)
        co.status = "Approved"
        co.approved_date = date.today()
        co.approved_by = approved_by
        db.commit()
        return {"success": True}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════
#  FILE ATTACHMENTS (Transmittals, RFIs, Change Orders)
# ═══════════════════════════════════════════════════════════

@router.get("/doc-attachments/{parent_type}/{parent_id}")
def list_doc_attachments(parent_type: str, parent_id: int):
    """List attachments for a transmittal, rfi, or change_order."""
    db = get_db()
    try:
        atts = db.query(DocAttachment).filter(
            DocAttachment.parent_type == parent_type,
            DocAttachment.parent_id == parent_id,
        ).order_by(DocAttachment.sort_order).all()
        return [{
            "id": a.id, "filename": a.filename,
            "file_size": a.file_size, "file_type": a.file_type,
            "is_drawing": a.is_drawing, "drawing_id": a.drawing_id,
            "uploaded_at": a.uploaded_at.isoformat() if a.uploaded_at else None,
        } for a in atts]
    finally:
        db.close()


@router.post("/doc-attachments/{parent_type}/{parent_id}")
async def upload_doc_attachment(
    parent_type: str, parent_id: int,
    file: UploadFile = File(...),
):
    """Upload a file attachment."""
    import base64 as b64mod
    db = get_db()
    try:
        content = await file.read()
        encoded = b64mod.b64encode(content).decode('utf-8')
        ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else "bin"

        att = DocAttachment(
            parent_type=parent_type,
            parent_id=parent_id,
            filename=file.filename,
            file_data=encoded,
            file_size=len(content),
            file_type=ext,
        )
        db.add(att)
        db.commit()
        db.refresh(att)
        return {"id": att.id, "filename": att.filename, "file_size": att.file_size}
    finally:
        db.close()


@router.delete("/doc-attachments/{attachment_id}")
def delete_doc_attachment(attachment_id: int):
    db = get_db()
    try:
        att = db.query(DocAttachment).get(attachment_id)
        if not att:
            raise HTTPException(404)
        db.delete(att)
        db.commit()
        return {"success": True}
    finally:
        db.close()


@router.get("/doc-attachments/{attachment_id}/download")
def download_doc_attachment(attachment_id: int):
    db = get_db()
    try:
        att = db.query(DocAttachment).get(attachment_id)
        if not att or not att.file_data:
            raise HTTPException(404)
        return {"file_data": att.file_data, "filename": att.filename, "file_type": att.file_type}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════
#  TRANSMITTAL DRAWING ATTACHMENTS
# ═══════════════════════════════════════════════════════════

@router.post("/transmittals/{transmittal_id}/attach-drawings")
def attach_drawings_to_transmittal(
    transmittal_id: int,
    drawing_ids: str = Form(""),  # comma-separated drawing IDs
):
    """Attach project drawings to a transmittal."""
    import base64 as b64mod
    db = get_db()
    try:
        t = db.query(Transmittal).get(transmittal_id)
        if not t:
            raise HTTPException(404)

        ids = [int(x.strip()) for x in drawing_ids.split(",") if x.strip()]
        attached = 0
        dwg_numbers = []

        for did in ids:
            dwg = db.query(Drawing).get(did)
            if not dwg:
                continue

            # Check if already attached
            existing = db.query(DocAttachment).filter(
                DocAttachment.parent_type == "transmittal",
                DocAttachment.parent_id == transmittal_id,
                DocAttachment.drawing_id == did,
            ).first()
            if existing:
                continue

            att = DocAttachment(
                parent_type="transmittal",
                parent_id=transmittal_id,
                filename=f"{dwg.drawing_number}.pdf",
                file_data=dwg.pdf_data if dwg.pdf_data else None,
                file_size=len(dwg.pdf_data or "") * 3 // 4,  # approx decoded size
                file_type="pdf",
                is_drawing=True,
                drawing_id=did,
            )
            db.add(att)
            attached += 1
            dwg_numbers.append(dwg.drawing_number)

        # Update transmittal drawing numbers
        existing_nums = (t.drawing_numbers or "").split(",") if t.drawing_numbers else []
        existing_nums = [x.strip() for x in existing_nums if x.strip()]
        all_nums = list(set(existing_nums + dwg_numbers))
        t.drawing_numbers = ", ".join(sorted(all_nums))

        db.commit()
        return {"attached": attached, "drawing_numbers": dwg_numbers}
    finally:
        db.close()


@router.get("/transmittals/{transmittal_id}/download-zip")
def download_transmittal_zip(transmittal_id: int):
    """Download transmittal with attached drawings as zip (base64 encoded)."""
    import zipfile
    import base64 as b64mod
    db = get_db()
    try:
        t = db.query(Transmittal).get(transmittal_id)
        if not t:
            raise HTTPException(404)

        atts = db.query(DocAttachment).filter(
            DocAttachment.parent_type == "transmittal",
            DocAttachment.parent_id == transmittal_id,
        ).all()

        if not atts:
            raise HTTPException(404, "No attachments to download")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for att in atts:
                if att.file_data:
                    zf.writestr(att.filename or f"file_{att.id}", b64mod.b64decode(att.file_data))
        buf.seek(0)

        zip_b64 = b64mod.b64encode(buf.getvalue()).decode('utf-8')
        return {
            "zip_data": zip_b64,
            "filename": f"{t.transmittal_number or 'transmittal'}.zip",
        }
    finally:
        db.close()
