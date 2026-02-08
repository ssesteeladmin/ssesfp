"""
SSE Steel Project Tracker - Phase 2.5 Routes
Procurement-to-Production Material Lifecycle
"""
import os
import json
import uuid
import math
from datetime import datetime, date
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Form, Query, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from models import (
    Base, Project, Assembly, Part, Drawing
)
from models_phase25 import (
    Vendor, NestRun, NestRunItem, NestRunDrop,
    RFQv2, RFQItemv2, POv2, POItemv2,
    YardTag, DropTag, MaterialInventory,
    DocumentPacket, PacketAttachment,
    generate_barcode
)
# from nesting import solve_nesting

router = APIRouter(prefix="/api/v2", tags=["phase25"])

_SessionLocal = None

def set_session(session_maker):
    global _SessionLocal
    _SessionLocal = session_maker

def get_db():
    db = _SessionLocal()
    try:
        return db
    except:
        db.close()
        raise


# ═══════════════════════════════════════════════════════════════
#  VENDORS
# ═══════════════════════════════════════════════════════════════

class VendorCreate(BaseModel):
    name: str
    contact_name: str = ""
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    fax: str = ""
    email: str = ""
    default_terms: str = "Net 45 days"
    notes: str = ""

def _vendor_dict(v):
    return {
        "id": v.id, "name": v.name, "contact_name": v.contact_name,
        "address_line1": v.address_line1, "address_line2": v.address_line2,
        "city": v.city, "state": v.state, "zip_code": v.zip_code,
        "phone": v.phone, "fax": v.fax, "email": v.email,
        "default_terms": v.default_terms, "notes": v.notes,
        "active": v.active,
    }

@router.get("/vendors")
def list_vendors(active_only: bool = True):
    db = get_db()
    try:
        q = db.query(Vendor)
        if active_only:
            q = q.filter(Vendor.active == True)
        return [_vendor_dict(v) for v in q.order_by(Vendor.name).all()]
    finally:
        db.close()

@router.post("/vendors")
def create_vendor(data: VendorCreate):
    db = get_db()
    try:
        v = Vendor(**data.model_dump())
        db.add(v)
        db.commit()
        db.refresh(v)
        return _vendor_dict(v)
    finally:
        db.close()

@router.put("/vendors/{vendor_id}")
def update_vendor(vendor_id: int, data: VendorCreate):
    db = get_db()
    try:
        v = db.query(Vendor).get(vendor_id)
        if not v:
            raise HTTPException(404, "Vendor not found")
        for k, val in data.model_dump().items():
            setattr(v, k, val)
        db.commit()
        return _vendor_dict(v)
    finally:
        db.close()

@router.delete("/vendors/{vendor_id}")
def delete_vendor(vendor_id: int):
    db = get_db()
    try:
        v = db.query(Vendor).get(vendor_id)
        if not v:
            raise HTTPException(404)
        v.active = False
        db.commit()
        return {"success": True}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  NESTING (Enhanced with Cut Lock)
# ═══════════════════════════════════════════════════════════════

@router.get("/projects/{project_id}/nestable-parts")
def get_nestable_parts(project_id: int, shape: Optional[str] = None):
    """Get all parts eligible for nesting (not yet cut)."""
    db = get_db()
    try:
        q = db.query(Part).join(Assembly).filter(
            Assembly.project_id == project_id,
            or_(Part.is_hardware == False, Part.is_hardware.is_(None)),
        )
        if shape:
            q = q.filter(Part.shape == shape)

        parts = q.order_by(Part.shape, Part.dimensions, Part.length_inches.desc()).all()
        result = []
        for p in parts:
            asm = db.query(Assembly).get(p.assembly_id)
            # Check if already cut via nest run items
            already_nested = db.query(NestRunItem).filter(
                NestRunItem.part_id == p.id
            ).first()

            result.append({
                "id": p.id,
                "part_mark": p.part_mark,
                "assembly_id": p.assembly_id,
                "assembly_mark": asm.assembly_mark if asm else "",
                "is_main_member": p.is_main_member,
                "shape": p.shape,
                "dimensions": p.dimensions,
                "grade": p.grade,
                "length_inches": p.length_inches,
                "length_display": p.length_display,
                "quantity": p.quantity * (asm.assembly_quantity if asm else 1),
                "is_hardware": p.is_hardware,
                "is_nested": already_nested is not None,
                "is_locked": already_nested is not None,
            })
        return result
    finally:
        db.close()


@router.get("/projects/{project_id}/nestable-shapes")
def get_nestable_shapes(project_id: int):
    """Get distinct shape groups for nesting selection."""
    db = get_db()
    try:
        shapes = db.query(
            Part.shape, Part.dimensions, Part.grade,
            func.count(Part.id).label('part_count'),
            func.sum(Part.length_inches).label('total_length')
        ).join(Assembly).filter(
            Assembly.project_id == project_id,
            or_(Part.is_hardware == False, Part.is_hardware.is_(None)),
        ).group_by(Part.shape, Part.dimensions, Part.grade).all()

        return [{
            "shape": s.shape,
            "dimensions": s.dimensions,
            "grade": s.grade,
            "part_count": s.part_count,
            "total_length_ft": round((s.total_length or 0) / 12, 1),
            "key": f"{s.shape}|{s.dimensions}|{s.grade}",
        } for s in shapes]
    finally:
        db.close()


class NestRequest(BaseModel):
    part_ids: List[int]
    stock_length_inches: float = 480  # 40ft default
    operator: str = ""
    machine: str = ""


@router.post("/projects/{project_id}/run-nest")
def run_nest(project_id: int, data: NestRequest):
    """Execute nesting on selected parts. Locks parts after nesting."""
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404, "Project not found")

        # Validate parts aren't already nested
        parts_to_nest = []
        for pid in data.part_ids:
            part = db.query(Part).get(pid)
            if not part:
                continue
            already = db.query(NestRunItem).filter(NestRunItem.part_id == pid).first()
            if already:
                raise HTTPException(400, f"Part {part.part_mark} has already been nested/cut and is locked")
            asm = db.query(Assembly).get(part.assembly_id)
            parts_to_nest.append({
                "id": part.id,
                "part_mark": part.part_mark,
                "assembly_id": part.assembly_id,
                "assembly_mark": asm.assembly_mark if asm else "",
                "shape": part.shape,
                "dimensions": part.dimensions,
                "grade": part.grade,
                "length_inches": part.length_inches or 0,
                "length_display": part.length_display or "",
                "quantity": part.quantity * (asm.assembly_quantity if asm else 1),
            })

        if not parts_to_nest:
            raise HTTPException(400, "No valid parts to nest")

        # Group by shape+dimensions+grade for nesting
        groups = {}
        for p in parts_to_nest:
            key = f"{p['shape']}|{p['dimensions']}|{p['grade']}"
            if key not in groups:
                groups[key] = {"shape": p['shape'], "dimensions": p['dimensions'], "grade": p['grade'], "items": []}
            groups[key]["items"].append(p)

        # Run nesting algorithm per group
        all_results = []
        total_stock = 0
        total_used = 0
        total_waste = 0

        for key, group in groups.items():
            cut_lengths = []
            part_refs = []
            for item in group["items"]:
                for _ in range(item["quantity"]):
                    cut_lengths.append(item["length_inches"])
                    part_refs.append(item)

            if not cut_lengths:
                continue

            # Simple first-fit decreasing nesting
            stock_len = data.stock_length_inches
            kerf = 0.25  # 1/4 inch saw kerf

            indexed = sorted(enumerate(cut_lengths), key=lambda x: -x[1])
            bins = []  # each bin: {"remaining": float, "cuts": [(idx, length)]}

            for orig_idx, length in indexed:
                placed = False
                for b in bins:
                    if b["remaining"] >= length + kerf:
                        b["cuts"].append((orig_idx, length))
                        b["remaining"] -= (length + kerf)
                        placed = True
                        break
                if not placed:
                    bins.append({"remaining": stock_len - length - kerf, "cuts": [(orig_idx, length)]})

            total_stock += len(bins)
            for b in bins:
                used = stock_len - b["remaining"]
                total_used += used
                total_waste += b["remaining"]

            all_results.append({
                "shape": group["shape"],
                "dimensions": group["dimensions"],
                "grade": group["grade"],
                "stock_length": stock_len,
                "bins": bins,
                "part_refs": part_refs,
                "cut_lengths": cut_lengths,
            })

        yield_pct = round((total_used / (total_stock * data.stock_length_inches) * 100), 1) if total_stock > 0 else 0

        # Create nest run record
        nest_run = NestRun(
            job_id=project_id,
            operator=data.operator,
            machine=data.machine,
            status="complete",
            yield_percentage=yield_pct,
            total_stock_used=total_stock,
            total_parts_cut=len(data.part_ids),
        )
        db.add(nest_run)
        db.flush()

        # Create items and drops
        nest_items = []
        nest_drops = []
        for res in all_results:
            for bin_idx, b in enumerate(res["bins"]):
                for cut_idx, (orig_idx, length) in enumerate(b["cuts"]):
                    p_ref = res["part_refs"][orig_idx]
                    item = NestRunItem(
                        nest_run_id=nest_run.id,
                        part_id=p_ref["id"],
                        assembly_id=p_ref["assembly_id"],
                        stock_index=bin_idx,
                        cut_position=cut_idx,
                        cut_length_inches=length,
                        shape=res["shape"],
                        dimensions=res["dimensions"],
                        grade=res["grade"],
                        part_mark=p_ref["part_mark"],
                        assembly_mark=p_ref["assembly_mark"],
                        quantity=1,
                    )
                    db.add(item)
                    nest_items.append({
                        "part_mark": p_ref["part_mark"],
                        "assembly_mark": p_ref["assembly_mark"],
                        "length": p_ref["length_display"],
                        "stock_index": bin_idx,
                        "cut_position": cut_idx,
                    })

                # Drop for this stock piece
                drop_inches = b["remaining"]
                if drop_inches > 6:  # only track drops > 6 inches
                    ft = int(drop_inches // 12)
                    inches = round(drop_inches % 12, 1)
                    drop_display = f"{ft}'-{inches}\"" if ft > 0 else f'{inches}"'
                    drop = NestRunDrop(
                        nest_run_id=nest_run.id,
                        stock_index=bin_idx,
                        shape=res["shape"],
                        dimensions=res["dimensions"],
                        grade=res["grade"],
                        stock_length_inches=res["stock_length"],
                        drop_length_inches=drop_inches,
                        drop_length_display=drop_display,
                    )
                    db.add(drop)
                    nest_drops.append({
                        "stock_index": bin_idx,
                        "shape": f"{res['shape']} {res['dimensions']}",
                        "grade": res["grade"],
                        "drop_length": drop_display,
                        "drop_inches": round(drop_inches, 1),
                    })

        db.commit()

        return {
            "nest_run_id": nest_run.id,
            "yield_percentage": yield_pct,
            "total_stock_pieces": total_stock,
            "total_parts_nested": len(data.part_ids),
            "items": nest_items,
            "drops": nest_drops,
            "groups": [{
                "shape": r["shape"],
                "dimensions": r["dimensions"],
                "grade": r["grade"],
                "stock_count": len(r["bins"]),
                "stock_length_ft": round(r["stock_length"] / 12, 1),
            } for r in all_results],
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Nesting error: {str(e)}")
    finally:
        db.close()


@router.get("/nest-runs/{nest_run_id}")
def get_nest_run(nest_run_id: int):
    """Get full nest run details."""
    db = get_db()
    try:
        nr = db.query(NestRun).get(nest_run_id)
        if not nr:
            raise HTTPException(404)

        items = db.query(NestRunItem).filter(NestRunItem.nest_run_id == nr.id).all()
        drops = db.query(NestRunDrop).filter(NestRunDrop.nest_run_id == nr.id).all()

        return {
            "id": nr.id,
            "job_id": nr.job_id,
            "nest_date": nr.nest_date.isoformat() if nr.nest_date else None,
            "operator": nr.operator,
            "machine": nr.machine,
            "status": nr.status,
            "yield_percentage": float(nr.yield_percentage) if nr.yield_percentage else 0,
            "total_stock_used": nr.total_stock_used,
            "total_parts_cut": nr.total_parts_cut,
            "items": [{
                "id": i.id, "part_mark": i.part_mark, "assembly_mark": i.assembly_mark,
                "shape": i.shape, "dimensions": i.dimensions, "grade": i.grade,
                "stock_index": i.stock_index, "cut_position": i.cut_position,
                "cut_length_inches": i.cut_length_inches,
            } for i in items],
            "drops": [{
                "id": d.id, "stock_index": d.stock_index,
                "shape": d.shape, "dimensions": d.dimensions, "grade": d.grade,
                "drop_length_display": d.drop_length_display,
                "drop_length_inches": d.drop_length_inches,
                "disposition": d.disposition,
                "disposition_by": d.disposition_by,
            } for d in drops],
        }
    finally:
        db.close()


@router.get("/projects/{project_id}/nest-runs")
def list_nest_runs(project_id: int):
    db = get_db()
    try:
        runs = db.query(NestRun).filter(NestRun.job_id == project_id).order_by(desc(NestRun.nest_date)).all()
        return [{
            "id": nr.id,
            "nest_date": nr.nest_date.isoformat() if nr.nest_date else None,
            "operator": nr.operator,
            "status": nr.status,
            "yield_percentage": float(nr.yield_percentage) if nr.yield_percentage else 0,
            "total_stock_used": nr.total_stock_used,
            "total_parts_cut": nr.total_parts_cut,
        } for nr in runs]
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  DROP DISPOSITION (Operator)
# ═══════════════════════════════════════════════════════════════

@router.post("/drops/{drop_id}/disposition")
def disposition_drop(
    drop_id: int,
    action: str = Form(...),  # 'inventory' or 'scrap'
    location: str = Form(""),
    operator: str = Form(""),
):
    db = get_db()
    try:
        drop = db.query(NestRunDrop).get(drop_id)
        if not drop:
            raise HTTPException(404, "Drop not found")
        if drop.disposition:
            raise HTTPException(400, "Drop already dispositioned")

        drop.disposition = action
        drop.disposition_date = datetime.utcnow()
        drop.disposition_by = operator

        if action == "inventory":
            drop.inventory_location = location
            # Create inventory record
            inv = MaterialInventory(
                source_type="drop",
                member_size=f"{drop.shape} {drop.dimensions}",
                shape=drop.shape,
                dimensions=drop.dimensions,
                length_display=drop.drop_length_display,
                length_inches=drop.drop_length_inches,
                grade=drop.grade,
                heat_number=drop.heat_number,
                location=location,
                status="available",
                added_by=operator,
            )
            db.add(inv)

        db.commit()
        return {"success": True, "action": action}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  RFQ SYSTEM
# ═══════════════════════════════════════════════════════════════

class RFQCreateFromNest(BaseModel):
    nest_run_id: int
    vendor_id: Optional[int] = None
    exclude_hardware: bool = True
    item_ids: List[int] = []  # specific nest item IDs; empty = all


@router.post("/projects/{project_id}/rfqs")
def create_rfq(project_id: int, data: RFQCreateFromNest):
    """Create an RFQ from nest run results."""
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)

        # Generate RFQ number
        count = db.query(RFQv2).filter(RFQv2.job_id == project_id).count()
        rfq_number = f"{project.job_number}-RFQ{count + 1:02d}"

        rfq = RFQv2(
            job_id=project_id,
            nest_run_id=data.nest_run_id,
            rfq_number=rfq_number,
            vendor_id=data.vendor_id,
            status="draft",
        )
        db.add(rfq)
        db.flush()

        # Get nest items
        q = db.query(NestRunItem).filter(NestRunItem.nest_run_id == data.nest_run_id)
        if data.item_ids:
            q = q.filter(NestRunItem.id.in_(data.item_ids))
        nest_items = q.all()

        # Consolidate by shape/dimensions/grade - sum up stock pieces needed
        material = {}
        for ni in nest_items:
            key = f"{ni.shape}|{ni.dimensions}|{ni.grade}"
            if key not in material:
                material[key] = {
                    "shape": ni.shape, "dimensions": ni.dimensions, "grade": ni.grade,
                    "stock_indices": set(), "total_qty": 0,
                }
            material[key]["stock_indices"].add(ni.stock_index)
            material[key]["total_qty"] += 1

        # Get stock length from nest run drops
        nest_run = db.query(NestRun).get(data.nest_run_id)
        stock_length_display = "40'-0\""

        line_num = 0
        items_created = []
        for key, mat in material.items():
            line_num += 1
            is_hw = mat["shape"] in ("HS", "NU", "WA", "MB", "ROD")  # hardware shapes
            qty = len(mat["stock_indices"])  # number of stock pieces

            item = RFQItemv2(
                rfq_id=rfq.id,
                line_number=line_num,
                qty=qty,
                shape=mat["shape"],
                dimensions=mat["dimensions"],
                grade=mat["grade"],
                length_display=stock_length_display,
                job_number=project.job_number,
                is_hardware=is_hw,
                excluded=is_hw and data.exclude_hardware,
            )
            db.add(item)
            items_created.append({
                "line": line_num,
                "qty": qty,
                "shape": mat["shape"],
                "dimensions": mat["dimensions"],
                "grade": mat["grade"],
                "is_hardware": is_hw,
                "excluded": is_hw and data.exclude_hardware,
            })

        db.commit()
        return {
            "rfq_id": rfq.id,
            "rfq_number": rfq_number,
            "items": items_created,
        }
    finally:
        db.close()


@router.get("/projects/{project_id}/rfqs-list")
def list_rfqs(project_id: int):
    db = get_db()
    try:
        rfqs = db.query(RFQv2).filter(RFQv2.job_id == project_id).order_by(desc(RFQv2.created_at)).all()
        result = []
        for r in rfqs:
            vendor = db.query(Vendor).get(r.vendor_id) if r.vendor_id else None
            items = db.query(RFQItemv2).filter(RFQItemv2.rfq_id == r.id, RFQItemv2.excluded == False).all()
            result.append({
                "id": r.id,
                "rfq_number": r.rfq_number,
                "vendor_name": vendor.name if vendor else "Unassigned",
                "vendor_id": r.vendor_id,
                "status": r.status,
                "item_count": len(items),
                "total_price": float(r.total_price) if r.total_price else 0,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return result
    finally:
        db.close()


@router.get("/rfqs/{rfq_id}")
def get_rfq(rfq_id: int):
    db = get_db()
    try:
        r = db.query(RFQv2).get(rfq_id)
        if not r:
            raise HTTPException(404)
        vendor = db.query(Vendor).get(r.vendor_id) if r.vendor_id else None
        project = db.query(Project).get(r.job_id)
        items = db.query(RFQItemv2).filter(RFQItemv2.rfq_id == r.id).order_by(RFQItemv2.line_number).all()

        return {
            "id": r.id,
            "rfq_number": r.rfq_number,
            "job_number": project.job_number if project else "",
            "project_name": project.project_name if project else "",
            "vendor": _vendor_dict(vendor) if vendor else None,
            "status": r.status,
            "sub_total": float(r.sub_total) if r.sub_total else 0,
            "tax": float(r.tax) if r.tax else 0,
            "freight": float(r.freight) if r.freight else 0,
            "misc_cost": float(r.misc_cost) if r.misc_cost else 0,
            "total_price": float(r.total_price) if r.total_price else 0,
            "terms_discount": float(r.terms_discount) if r.terms_discount else 0,
            "items": [{
                "id": i.id,
                "line_number": i.line_number,
                "qty": i.qty,
                "shape": i.shape,
                "dimensions": i.dimensions,
                "grade": i.grade,
                "length_display": i.length_display,
                "job_number": i.job_number,
                "weight": float(i.weight) if i.weight else 0,
                "unit_price": float(i.unit_price) if i.unit_price else 0,
                "unit_type": i.unit_type,
                "total_price": float(i.total_price) if i.total_price else 0,
                "excluded": i.excluded,
                "is_hardware": i.is_hardware,
            } for i in items],
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
    finally:
        db.close()


@router.put("/rfqs/{rfq_id}/assign-vendor")
def assign_rfq_vendor(rfq_id: int, vendor_id: int = Form(...)):
    db = get_db()
    try:
        r = db.query(RFQv2).get(rfq_id)
        if not r:
            raise HTTPException(404)
        r.vendor_id = vendor_id
        db.commit()
        return {"success": True}
    finally:
        db.close()


@router.put("/rfqs/{rfq_id}/toggle-item/{item_id}")
def toggle_rfq_item(rfq_id: int, item_id: int):
    """Toggle exclude/include on an RFQ item."""
    db = get_db()
    try:
        item = db.query(RFQItemv2).get(item_id)
        if not item or item.rfq_id != rfq_id:
            raise HTTPException(404)
        item.excluded = not item.excluded
        db.commit()
        return {"excluded": item.excluded}
    finally:
        db.close()


@router.put("/rfqs/{rfq_id}/update-status")
def update_rfq_status(rfq_id: int, status: str = Form(...)):
    db = get_db()
    try:
        r = db.query(RFQv2).get(rfq_id)
        if not r:
            raise HTTPException(404)
        r.status = status
        if status == "sent":
            r.date_sent = datetime.utcnow()
        db.commit()
        return {"success": True}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  PURCHASE ORDERS
# ═══════════════════════════════════════════════════════════════

@router.post("/rfqs/{rfq_id}/convert-to-po")
def convert_rfq_to_po(rfq_id: int, ordered_by: str = Form("")):
    """Convert an accepted RFQ into a Purchase Order."""
    db = get_db()
    try:
        rfq = db.query(RFQv2).get(rfq_id)
        if not rfq:
            raise HTTPException(404)

        project = db.query(Project).get(rfq.job_id)
        vendor = db.query(Vendor).get(rfq.vendor_id) if rfq.vendor_id else None

        # Generate PO number
        count = db.query(POv2).filter(POv2.job_id == rfq.job_id).count()
        po_number = f"{project.job_number}-PO{count + 1:02d}" if project else f"PO-{count + 1:04d}"

        po = POv2(
            job_id=rfq.job_id,
            rfq_id=rfq.id,
            po_number=po_number,
            vendor_id=rfq.vendor_id,
            ordered_by=ordered_by,
            order_date=date.today(),
            terms=vendor.default_terms if vendor else "Net 45 days",
            sub_total=rfq.sub_total,
            tax=rfq.tax,
            total_price=rfq.total_price,
            status="draft",
        )
        db.add(po)
        db.flush()

        # Copy non-excluded items
        rfq_items = db.query(RFQItemv2).filter(
            RFQItemv2.rfq_id == rfq.id,
            RFQItemv2.excluded == False,
        ).order_by(RFQItemv2.line_number).all()

        line = 0
        for ri in rfq_items:
            line += 1
            barcode = generate_barcode()
            poi = POItemv2(
                po_id=po.id,
                line_number=line,
                qty=ri.qty,
                shape=ri.shape,
                dimensions=ri.dimensions,
                grade=ri.grade,
                length_display=ri.length_display,
                length_inches=ri.length_inches,
                job_number=ri.job_number,
                weight=ri.weight,
                unit_cost=ri.unit_price,
                unit_type=ri.unit_type,
                cost=ri.total_price,
                receiving_barcode=barcode,
            )
            db.add(poi)

        rfq.status = "accepted"
        db.commit()

        return {"po_id": po.id, "po_number": po_number}
    finally:
        db.close()


@router.get("/projects/{project_id}/pos-list")
def list_pos(project_id: int):
    db = get_db()
    try:
        pos = db.query(POv2).filter(POv2.job_id == project_id).order_by(desc(POv2.created_at)).all()
        result = []
        for po in pos:
            vendor = db.query(Vendor).get(po.vendor_id) if po.vendor_id else None
            items = db.query(POItemv2).filter(POItemv2.po_id == po.id).all()
            received = sum(1 for i in items if i.receiving_status == "complete")
            result.append({
                "id": po.id,
                "po_number": po.po_number,
                "vendor_name": vendor.name if vendor else "",
                "status": po.status,
                "order_date": po.order_date.isoformat() if po.order_date else None,
                "total_price": float(po.total_price) if po.total_price else 0,
                "items_total": len(items),
                "items_received": received,
            })
        return result
    finally:
        db.close()


@router.get("/pos/{po_id}")
def get_po(po_id: int):
    db = get_db()
    try:
        po = db.query(POv2).get(po_id)
        if not po:
            raise HTTPException(404)
        vendor = db.query(Vendor).get(po.vendor_id) if po.vendor_id else None
        project = db.query(Project).get(po.job_id)
        items = db.query(POItemv2).filter(POItemv2.po_id == po.id).order_by(POItemv2.line_number).all()

        return {
            "id": po.id,
            "po_number": po.po_number,
            "job_number": project.job_number if project else "",
            "project_name": project.project_name if project else "",
            "vendor": _vendor_dict(vendor) if vendor else None,
            "ordered_by": po.ordered_by,
            "order_date": po.order_date.isoformat() if po.order_date else None,
            "fob": po.fob,
            "ship_via": po.ship_via,
            "terms": po.terms,
            "order_type": po.order_type,
            "sub_total": float(po.sub_total) if po.sub_total else 0,
            "tax": float(po.tax) if po.tax else 0,
            "total_price": float(po.total_price) if po.total_price else 0,
            "status": po.status,
            "items": [{
                "id": i.id,
                "line_number": i.line_number,
                "qty": i.qty,
                "shape": i.shape,
                "dimensions": i.dimensions,
                "grade": i.grade,
                "length_display": i.length_display,
                "job_number": i.job_number,
                "weight": float(i.weight) if i.weight else 0,
                "unit_cost": float(i.unit_cost) if i.unit_cost else 0,
                "unit_type": i.unit_type,
                "cost": float(i.cost) if i.cost else 0,
                "qty_received": i.qty_received,
                "heat_number": i.heat_number,
                "receiving_barcode": i.receiving_barcode,
                "receiving_status": i.receiving_status,
                "date_received": i.date_received.isoformat() if i.date_received else None,
            } for i in items],
        }
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  RECEIVING & TAGS
# ═══════════════════════════════════════════════════════════════

@router.post("/po-items/{item_id}/receive")
def receive_po_item(
    item_id: int,
    qty_received: int = Form(...),
    heat_number: str = Form(...),
    received_by: str = Form(""),
    yard_location: str = Form(""),
):
    """Check in a PO item, assign heat #, auto-generate yard tag + drop tag."""
    db = get_db()
    try:
        item = db.query(POItemv2).get(item_id)
        if not item:
            raise HTTPException(404, "PO item not found")

        po = db.query(POv2).get(item.po_id)
        vendor = db.query(Vendor).get(po.vendor_id) if po and po.vendor_id else None

        item.qty_received = qty_received
        item.heat_number = heat_number
        item.received_by = received_by
        item.date_received = date.today()
        item.receiving_status = "complete" if qty_received >= item.qty else "partial"

        # Generate Yard Tag immediately
        yt_barcode = f"YT-{generate_barcode()}"
        yard_tag = YardTag(
            po_item_id=item.id,
            tag_barcode=yt_barcode,
            member_size=f"{item.shape} {item.dimensions}",
            length_display=item.length_display,
            weight=item.weight,
            grade=item.grade,
            heat_number=heat_number,
            supplier=vendor.name if vendor else "",
            po_number=po.po_number if po else "",
            job_number=item.job_number,
            yard_location=yard_location,
        )
        db.add(yard_tag)
        db.flush()

        # Generate Drop Tag (pre-created, drop length populated later from nest)
        dt_barcode = f"DT-{generate_barcode()}"
        drop_tag = DropTag(
            yard_tag_id=yard_tag.id,
            tag_barcode=dt_barcode,
            member_size=f"{item.shape} {item.dimensions}",
            grade=item.grade,
            heat_number=heat_number,
            source_po=po.po_number if po else "",
        )
        db.add(drop_tag)

        # Update PO status
        all_items = db.query(POItemv2).filter(POItemv2.po_id == item.po_id).all()
        all_received = all(i.receiving_status == "complete" for i in all_items)
        any_received = any(i.receiving_status in ("complete", "partial") for i in all_items)

        if all_received:
            po.status = "complete"
        elif any_received:
            po.status = "partial"

        db.commit()

        return {
            "success": True,
            "yard_tag": {
                "id": yard_tag.id,
                "barcode": yt_barcode,
                "member_size": yard_tag.member_size,
                "heat_number": heat_number,
                "yard_location": yard_location,
            },
            "drop_tag": {
                "id": drop_tag.id,
                "barcode": dt_barcode,
            },
            "po_status": po.status,
        }
    finally:
        db.close()


@router.get("/pos/{po_id}/receiving-checklist")
def get_receiving_checklist(po_id: int):
    """Get receiving checklist with barcodes for a PO."""
    db = get_db()
    try:
        po = db.query(POv2).get(po_id)
        if not po:
            raise HTTPException(404)

        items = db.query(POItemv2).filter(POItemv2.po_id == po.id).order_by(POItemv2.line_number).all()
        vendor = db.query(Vendor).get(po.vendor_id) if po.vendor_id else None

        return {
            "po_number": po.po_number,
            "vendor_name": vendor.name if vendor else "",
            "items": [{
                "id": i.id,
                "line_number": i.line_number,
                "qty": i.qty,
                "dimensions": f"{i.shape} {i.dimensions}",
                "grade": i.grade,
                "length": i.length_display,
                "barcode": i.receiving_barcode,
                "qty_received": i.qty_received,
                "heat_number": i.heat_number,
                "receiving_status": i.receiving_status,
                "date_received": i.date_received.isoformat() if i.date_received else None,
            } for i in items],
        }
    finally:
        db.close()


@router.post("/yard-tags/{barcode}/scan-start-cut")
def scan_yard_tag_start_cut(barcode: str, operator: str = Form("")):
    """Operator scans yard tag to signal start of cutting."""
    db = get_db()
    try:
        tag = db.query(YardTag).filter(YardTag.tag_barcode == barcode).first()
        if not tag:
            raise HTTPException(404, "Yard tag not found")
        if tag.status == "cut_complete":
            raise HTTPException(400, "This piece has already been cut")

        tag.status = "cutting"
        tag.scan_start_cut = datetime.utcnow()
        tag.cut_by = operator
        db.commit()

        return {
            "success": True,
            "member_size": tag.member_size,
            "length": tag.length_display,
            "grade": tag.grade,
            "heat_number": tag.heat_number,
            "job_number": tag.job_number,
        }
    finally:
        db.close()


@router.post("/drop-tags/{barcode}/scan-to-inventory")
def scan_drop_tag_to_inventory(
    barcode: str,
    drop_length: str = Form(""),
    drop_length_inches: float = Form(0),
    location: str = Form(""),
    operator: str = Form(""),
):
    """Operator scans drop tag after cutting to add to inventory."""
    db = get_db()
    try:
        tag = db.query(DropTag).filter(DropTag.tag_barcode == barcode).first()
        if not tag:
            raise HTTPException(404, "Drop tag not found")

        tag.disposition = "inventory"
        tag.disposition_date = datetime.utcnow()
        tag.disposition_by = operator
        tag.drop_length_display = drop_length
        tag.drop_length_inches = drop_length_inches
        tag.inventory_location = location

        # Create inventory record
        inv = MaterialInventory(
            source_type="drop",
            drop_tag_id=tag.id,
            member_size=tag.member_size,
            shape=tag.member_size.split()[0] if tag.member_size else "",
            length_display=drop_length,
            length_inches=drop_length_inches,
            grade=tag.grade,
            heat_number=tag.heat_number,
            location=location,
            added_by=operator,
        )
        db.add(inv)

        # Mark yard tag as cut complete
        if tag.yard_tag_id:
            yt = db.query(YardTag).get(tag.yard_tag_id)
            if yt:
                yt.status = "cut_complete"

        db.commit()
        return {"success": True, "inventory_id": inv.id}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  MATERIAL INVENTORY
# ═══════════════════════════════════════════════════════════════

@router.get("/inventory-v2")
def list_material_inventory(shape: Optional[str] = None, status: str = "available"):
    db = get_db()
    try:
        q = db.query(MaterialInventory).filter(MaterialInventory.status == status)
        if shape:
            q = q.filter(MaterialInventory.shape == shape)
        items = q.order_by(MaterialInventory.member_size, MaterialInventory.length_inches.desc()).all()
        return [{
            "id": i.id,
            "source_type": i.source_type,
            "member_size": i.member_size,
            "length_display": i.length_display,
            "length_inches": i.length_inches,
            "weight": float(i.weight) if i.weight else 0,
            "grade": i.grade,
            "heat_number": i.heat_number,
            "location": i.location,
            "status": i.status,
            "added_date": i.added_date.isoformat() if i.added_date else None,
        } for i in items]
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  DOCUMENT PACKETS (Transmittals, COs, RFIs)
# ═══════════════════════════════════════════════════════════════

class PacketCreate(BaseModel):
    doc_type: str  # transmittal, change_order, rfi
    to_company: str = ""
    to_contact: str = ""
    to_address: str = ""
    to_phone: str = ""
    to_fax: str = ""
    to_email: str = ""
    subject: str = ""
    description: str = ""
    # CO fields
    co_shop_drawings: float = 0
    co_material: float = 0
    co_fabrication: float = 0
    co_coating: float = 0
    co_field_work: float = 0
    co_overhead_pct: float = 15
    # Transmittal fields
    transmittal_items: Optional[List[dict]] = None
    prints_enclosed: int = 0


@router.post("/projects/{project_id}/packets")
def create_packet(project_id: int, data: PacketCreate):
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)

        # Generate doc number
        count = db.query(DocumentPacket).filter(
            DocumentPacket.job_id == project_id,
            DocumentPacket.doc_type == data.doc_type,
        ).count()

        prefix = {"transmittal": "T", "change_order": "CO", "rfi": "RFI"}.get(data.doc_type, "DOC")
        doc_number = f"{project.job_number}-{prefix}{count + 1:02d}"

        # Calculate CO total
        co_subtotal = data.co_shop_drawings + data.co_material + data.co_fabrication + data.co_coating + data.co_field_work
        co_overhead = co_subtotal * (data.co_overhead_pct / 100)
        co_total = co_subtotal + co_overhead

        packet = DocumentPacket(
            job_id=project_id,
            doc_type=data.doc_type,
            doc_number=doc_number,
            to_company=data.to_company,
            to_contact=data.to_contact,
            to_address=data.to_address,
            to_phone=data.to_phone,
            to_fax=data.to_fax,
            to_email=data.to_email,
            subject=data.subject,
            description=data.description,
            co_shop_drawings=data.co_shop_drawings,
            co_material=data.co_material,
            co_fabrication=data.co_fabrication,
            co_coating=data.co_coating,
            co_field_work=data.co_field_work,
            co_overhead_pct=data.co_overhead_pct,
            co_total=co_total,
            transmittal_items=data.transmittal_items,
            prints_enclosed=data.prints_enclosed,
        )
        db.add(packet)
        db.commit()
        db.refresh(packet)

        return {"id": packet.id, "doc_number": doc_number}
    finally:
        db.close()


@router.get("/projects/{project_id}/packets")
def list_packets(project_id: int, doc_type: Optional[str] = None):
    db = get_db()
    try:
        q = db.query(DocumentPacket).filter(DocumentPacket.job_id == project_id)
        if doc_type:
            q = q.filter(DocumentPacket.doc_type == doc_type)
        packets = q.order_by(desc(DocumentPacket.created_at)).all()
        return [{
            "id": p.id,
            "doc_type": p.doc_type,
            "doc_number": p.doc_number,
            "to_company": p.to_company,
            "subject": p.subject,
            "status": p.status,
            "attachment_count": p.attachment_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "co_total": float(p.co_total) if p.co_total else 0,
        } for p in packets]
    finally:
        db.close()


@router.get("/packets/{packet_id}")
def get_packet(packet_id: int):
    db = get_db()
    try:
        p = db.query(DocumentPacket).get(packet_id)
        if not p:
            raise HTTPException(404)
        project = db.query(Project).get(p.job_id)
        attachments = db.query(PacketAttachment).filter(
            PacketAttachment.packet_id == p.id
        ).order_by(PacketAttachment.sort_order).all()

        return {
            "id": p.id,
            "doc_type": p.doc_type,
            "doc_number": p.doc_number,
            "job_number": project.job_number if project else "",
            "project_name": project.project_name if project else "",
            "to_company": p.to_company,
            "to_contact": p.to_contact,
            "to_address": p.to_address,
            "to_phone": p.to_phone,
            "to_email": p.to_email,
            "subject": p.subject,
            "description": p.description,
            "co_shop_drawings": float(p.co_shop_drawings) if p.co_shop_drawings else 0,
            "co_material": float(p.co_material) if p.co_material else 0,
            "co_fabrication": float(p.co_fabrication) if p.co_fabrication else 0,
            "co_coating": float(p.co_coating) if p.co_coating else 0,
            "co_field_work": float(p.co_field_work) if p.co_field_work else 0,
            "co_overhead_pct": float(p.co_overhead_pct) if p.co_overhead_pct else 15,
            "co_total": float(p.co_total) if p.co_total else 0,
            "transmittal_items": p.transmittal_items,
            "prints_enclosed": p.prints_enclosed,
            "attachment_count": p.attachment_count,
            "attachments": [{
                "id": a.id, "filename": a.filename,
                "file_size": a.file_size, "sort_order": a.sort_order,
            } for a in attachments],
            "status": p.status,
            "date_sent": p.date_sent.isoformat() if p.date_sent else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
    finally:
        db.close()


@router.post("/packets/{packet_id}/attach")
async def attach_file(packet_id: int, file: UploadFile = File(...)):
    db = get_db()
    try:
        packet = db.query(DocumentPacket).get(packet_id)
        if not packet:
            raise HTTPException(404)

        import base64
        content = await file.read()
        b64 = base64.b64encode(content).decode("utf-8")

        att = PacketAttachment(
            packet_id=packet_id,
            filename=file.filename,
            file_data=b64,
            file_size=len(content),
            sort_order=packet.attachment_count,
        )
        db.add(att)
        packet.attachment_count = (packet.attachment_count or 0) + 1
        db.commit()

        return {"id": att.id, "filename": att.filename, "attachment_count": packet.attachment_count}
    finally:
        db.close()


@router.put("/packets/{packet_id}/send")
def send_packet(packet_id: int):
    db = get_db()
    try:
        p = db.query(DocumentPacket).get(packet_id)
        if not p:
            raise HTTPException(404)
        p.status = "sent"
        p.date_sent = datetime.utcnow()
        db.commit()
        return {"success": True}
    finally:
        db.close()
