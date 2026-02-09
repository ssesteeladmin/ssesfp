"""
SSE Steel Project Tracker - Phase 2.5 Routes
Procurement-to-Production Material Lifecycle
"""
import os
import io
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
    RFQQuote, RFQQuoteItem,
    YardTag, DropTag, MaterialInventory,
    DocumentPacket, PacketAttachment, StockConfig,
    generate_barcode
)
# nesting is handled inline in run_nest endpoint

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


@router.post("/vendors/import-csv")
async def import_vendors_csv(file: UploadFile = File(...)):
    """Bulk import vendors from CSV. Expected columns: Name, Contact, Address, City, State, Zip, Phone, Fax, Email, Terms"""
    import csv, io as iomod
    db = get_db()
    try:
        content = await file.read()
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(iomod.StringIO(text))
        
        imported = 0
        skipped = 0
        for row in reader:
            # Flexible column matching - try common header variations
            name = row.get("Name") or row.get("name") or row.get("Company") or row.get("company") or row.get("Vendor") or row.get("vendor") or ""
            if not name.strip():
                skipped += 1
                continue
            
            # Check if vendor already exists
            existing = db.query(Vendor).filter(Vendor.name == name.strip()).first()
            if existing:
                skipped += 1
                continue
            
            v = Vendor(
                name=name.strip(),
                contact_name=(row.get("Contact") or row.get("contact_name") or row.get("ContactName") or "").strip(),
                address_line1=(row.get("Address") or row.get("address") or row.get("Address1") or "").strip(),
                city=(row.get("City") or row.get("city") or "").strip(),
                state=(row.get("State") or row.get("state") or "").strip(),
                zip_code=(row.get("Zip") or row.get("zip") or row.get("ZipCode") or row.get("zip_code") or "").strip(),
                phone=(row.get("Phone") or row.get("phone") or "").strip(),
                fax=(row.get("Fax") or row.get("fax") or "").strip(),
                email=(row.get("Email") or row.get("email") or "").strip(),
                default_terms=(row.get("Terms") or row.get("terms") or "Net 45 days").strip(),
            )
            db.add(v)
            imported += 1
        
        db.commit()
        return {"imported": imported, "skipped": skipped}
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
                "width_inches": p.width_inches or 0,
                "quantity": p.quantity * (asm.assembly_quantity if asm else 1),
                "is_hardware": p.is_hardware,
                "is_anchor_bolt": getattr(p, 'is_anchor_bolt', False),
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
    stock_length_inches: float = 0  # 0 = auto from stock config
    operator: str = ""
    machine: str = ""
    nest_mode: str = "mult"  # mult, plate, both


@router.post("/projects/{project_id}/run-nest")
def run_nest(project_id: int, data: NestRequest):
    """Execute nesting on selected parts with mixed-length optimization."""
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
                "width_inches": part.width_inches or 0,
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

        # Shape to stock config mapping
        shape_map = {"W": "W", "WF": "W", "HSS": "HSS", "TS": "HSS", "HSSR": "HSSR", "RT": "RT",
                     "PIPE": "PIPE", "L": "L", "C": "C", "MC": "MC", "S": "S", "PL": "PL", "PLATE": "PL"}

        PLATE_SHAPES = ("PL", "PLATE")
        all_results = []
        total_used = 0
        total_material = 0
        buy_list = []  # consolidated purchase list

        for key, group in groups.items():
            is_plate = group["shape"].upper() in PLATE_SHAPES

            # Get stock config
            mapped_shape = shape_map.get(group["shape"].upper(), group["shape"].upper())
            stock_cfg = db.query(StockConfig).filter(
                StockConfig.shape_code == mapped_shape,
                StockConfig.active == True
            ).first()
            kerf = stock_cfg.kerf_inches if stock_cfg else 0.125

            if is_plate and data.nest_mode != "mult":
                # ═══ PLATE NESTING (2D strip packing) ═══
                plates = []
                part_refs = []
                for item in group["items"]:
                    for _ in range(item["quantity"]):
                        w = item["width_inches"] or 12  # default 12" if no width
                        l = item["length_inches"] or 12
                        # Ensure w <= l (width is shorter side)
                        if w > l:
                            w, l = l, w
                        plates.append({"width": w, "length": l})
                        part_refs.append(item)

                if not plates:
                    continue

                # Determine thickness for sheet size selection
                thickness = _parse_plate_thickness(group["dimensions"])
                available_sheets = _get_plate_sheets(stock_cfg, thickness)

                # Try each sheet size, pick best utilization
                best = None
                for sheet in available_sheets:
                    sw = sheet["w"] * 12  # feet to inches
                    sl = sheet["l"] * 12

                    # Strip packing: sort plates by width descending
                    sorted_p = sorted(enumerate(plates), key=lambda x: -x[1]["width"])
                    sheets_used = []
                    cur = {"strips": [], "y_used": 0, "stock_w": sw, "stock_l": sl}

                    for orig_idx, pl in sorted_p:
                        pw, pl_len = pl["width"], pl["length"]
                        placed = False

                        # Try existing strips on current sheet
                        for strip in cur["strips"]:
                            if strip["x_rem"] >= pl_len + kerf and strip["h"] >= pw:
                                strip["cuts"].append((orig_idx, pw, pl_len))
                                strip["x_rem"] -= (pl_len + kerf)
                                placed = True
                                break

                        if not placed and cur["y_used"] + pw + kerf <= sw:
                            # New strip on current sheet
                            cur["strips"].append({
                                "h": pw, "x_rem": sl - pl_len - kerf,
                                "cuts": [(orig_idx, pw, pl_len)]
                            })
                            cur["y_used"] += pw + kerf
                            placed = True

                        if not placed:
                            # New sheet
                            sheets_used.append(cur)
                            cur = {
                                "strips": [{"h": pw, "x_rem": sl - pl_len - kerf, "cuts": [(orig_idx, pw, pl_len)]}],
                                "y_used": pw + kerf, "stock_w": sw, "stock_l": sl
                            }

                    sheets_used.append(cur)

                    total_sheet_area = len(sheets_used) * sw * sl
                    total_piece_area = sum(p["width"] * p["length"] for p in plates)
                    util = (total_piece_area / total_sheet_area * 100) if total_sheet_area > 0 else 0

                    if best is None or util > best["util"]:
                        best = {"sheet": sheet, "sheets_used": sheets_used, "util": util}

                if not best:
                    continue

                sheet = best["sheet"]
                sw_in = sheet["w"] * 12
                sl_in = sheet["l"] * 12
                n_sheets = len(best["sheets_used"])

                total_piece_area = sum(p["width"] * p["length"] for p in plates)
                total_sheet_area = n_sheets * sw_in * sl_in
                total_material += total_sheet_area
                total_used += total_piece_area

                buy_list.append({
                    "shape": group["shape"],
                    "dimensions": group["dimensions"],
                    "grade": group["grade"],
                    "stock_desc": f"{sheet['w']}'×{sheet['l']}' PL {group['dimensions']}",
                    "qty": n_sheets,
                    "stock_length_ft": sheet["l"],
                    "stock_width_ft": sheet["w"],
                    "is_plate": True,
                })

                # Store as nest result bins (one bin per sheet)
                bins_for_result = []
                for sh in best["sheets_used"]:
                    all_cuts = []
                    for strip in sh["strips"]:
                        for c in strip["cuts"]:
                            all_cuts.append(c)
                    bins_for_result.append({
                        "stock_length": sl_in,
                        "stock_width": sw_in,
                        "remaining": (sw_in * sl_in) - sum(c[1] * c[2] for c in all_cuts),
                        "cuts": [(c[0], c[2]) for c in all_cuts],  # (orig_idx, length)
                    })

                all_results.append({
                    "shape": group["shape"],
                    "dimensions": group["dimensions"],
                    "grade": group["grade"],
                    "is_plate": True,
                    "bins": bins_for_result,
                    "part_refs": part_refs,
                    "cut_lengths": [p["length"] for p in plates],
                    "sheet_desc": f"{sheet['w']}'×{sheet['l']}'",
                    "yield_pct": round(best["util"], 1),
                })

            else:
                # ═══ MULT NESTING (mixed-length 1D bin packing) ═══
                cut_lengths = []
                part_refs = []
                for item in group["items"]:
                    for _ in range(item["quantity"]):
                        cut_lengths.append(item["length_inches"])
                        part_refs.append(item)

                if not cut_lengths:
                    continue

                # Get available stock lengths in inches
                if data.stock_length_inches > 0:
                    avail_inches = [data.stock_length_inches]
                elif stock_cfg and stock_cfg.available_lengths:
                    avail_inches = sorted([l * 12 for l in stock_cfg.available_lengths])
                else:
                    avail_inches = [480]  # 40ft default

                # Mixed-length best-fit-decreasing bin packing
                indexed = sorted(enumerate(cut_lengths), key=lambda x: -x[1])
                bins = []

                for orig_idx, length in indexed:
                    # Best-fit: find bin with least remaining that still fits
                    best_bin = None
                    best_remaining = float('inf')
                    for b in bins:
                        if b["remaining"] >= length + kerf:
                            nr = b["remaining"] - length - kerf
                            if nr < best_remaining:
                                best_remaining = nr
                                best_bin = b

                    if best_bin:
                        best_bin["cuts"].append((orig_idx, length))
                        best_bin["remaining"] -= (length + kerf)
                    else:
                        # Open new bin - pick SMALLEST stock that fits
                        valid = [sl for sl in avail_inches if sl >= length + kerf]
                        if not valid:
                            stock_len = max(avail_inches)
                        else:
                            stock_len = valid[0]
                        bins.append({
                            "stock_length": stock_len,
                            "remaining": stock_len - length - kerf,
                            "cuts": [(orig_idx, length)]
                        })

                # Calculate totals for this group
                group_material = sum(b["stock_length"] for b in bins)
                group_used = sum(b["stock_length"] - b["remaining"] for b in bins)
                total_material += group_material
                total_used += group_used

                # Build buy list - consolidate by stock length
                length_counts = {}
                for b in bins:
                    sl_ft = round(b["stock_length"] / 12, 1)
                    length_counts[sl_ft] = length_counts.get(sl_ft, 0) + 1

                for sl_ft, qty in sorted(length_counts.items()):
                    buy_list.append({
                        "shape": group["shape"],
                        "dimensions": group["dimensions"],
                        "grade": group["grade"],
                        "stock_desc": f"{group['shape']} {group['dimensions']} × {sl_ft}'",
                        "qty": qty,
                        "stock_length_ft": sl_ft,
                        "is_plate": False,
                    })

                all_results.append({
                    "shape": group["shape"],
                    "dimensions": group["dimensions"],
                    "grade": group["grade"],
                    "is_plate": False,
                    "bins": bins,
                    "part_refs": part_refs,
                    "cut_lengths": cut_lengths,
                })

        yield_pct = round((total_used / total_material * 100), 1) if total_material > 0 else 0

        # Create nest run record
        total_stock = sum(len(r["bins"]) for r in all_results)
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
                stock_len = b.get("stock_length", 0)
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
                if drop_inches > 6:
                    ft = int(drop_inches // 12)
                    inches = round(drop_inches % 12, 1)
                    drop_display = f"{ft}'-{inches}\"" if ft > 0 else f'{inches}"'
                    drop = NestRunDrop(
                        nest_run_id=nest_run.id,
                        stock_index=bin_idx,
                        shape=res["shape"],
                        dimensions=res["dimensions"],
                        grade=res["grade"],
                        stock_length_inches=stock_len,
                        drop_length_inches=drop_inches,
                        drop_length_display=drop_display,
                    )
                    db.add(drop)
                    nest_drops.append({
                        "stock_index": bin_idx,
                        "shape": f"{res['shape']} {res['dimensions']}",
                        "grade": res["grade"],
                        "stock_length_ft": round(stock_len / 12, 1),
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
            "buy_list": buy_list,
            "groups": [{
                "shape": r["shape"],
                "dimensions": r["dimensions"],
                "grade": r["grade"],
                "is_plate": r.get("is_plate", False),
                "stock_count": len(r["bins"]),
                "yield_pct": r.get("yield_pct"),
                "sheet_desc": r.get("sheet_desc"),
                # For mult: show mixed lengths used
                "stock_lengths": list(set(round(b["stock_length"] / 12, 1) for b in r["bins"])) if not r.get("is_plate") else [],
            } for r in all_results],
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Nesting error: {str(e)}")
    finally:
        db.close()


def _parse_plate_thickness(dimensions_str):
    """Parse plate thickness from dimensions string like '1/2', '3/4', '1-1/4'."""
    s = (dimensions_str or "").strip()
    try:
        if "-" in s and "/" in s:
            # Mixed: "1-1/4" -> 1.25
            parts = s.split("-")
            whole = float(parts[0])
            num, den = parts[1].split("/")
            return whole + float(num) / float(den)
        elif "/" in s:
            num, den = s.split("/")
            return float(num) / float(den)
        else:
            return float(s) if s else 0.5
    except (ValueError, ZeroDivisionError):
        return 0.5  # default


def _get_plate_sheets(stock_cfg, thickness):
    """Get available plate sheet sizes for a given thickness."""
    if not stock_cfg or not stock_cfg.available_lengths:
        # Default sheets
        return [{"w": 4, "l": 8}, {"w": 5, "l": 10}]

    sheets = []
    for s in stock_cfg.available_lengths:
        if not isinstance(s, dict):
            continue
        t_min = s.get("thickness_min", 0)
        t_max = s.get("thickness_max", 99)
        if t_min <= thickness <= t_max:
            sheets.append({"w": s["w"], "l": s["l"]})

    return sheets if sheets else [{"w": 4, "l": 8}, {"w": 5, "l": 10}]


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


@router.delete("/nest-runs/{nest_run_id}")
def delete_nest_run(nest_run_id: int):
    """Delete an entire nest run — unlocks all parts so they can be re-nested."""
    db = get_db()
    try:
        nr = db.query(NestRun).get(nest_run_id)
        if not nr:
            raise HTTPException(404, "Nest run not found")

        # Delete drops (and any inventory created from them)
        drops = db.query(NestRunDrop).filter(NestRunDrop.nest_run_id == nest_run_id).all()
        for d in drops:
            if d.disposition == "inventory":
                db.query(MaterialInventory).filter(
                    MaterialInventory.source_type == "drop",
                    MaterialInventory.shape == d.shape,
                    MaterialInventory.dimensions == d.dimensions,
                    MaterialInventory.length_inches == d.drop_length_inches,
                ).delete()
            db.delete(d)

        # Delete nest items
        items = db.query(NestRunItem).filter(NestRunItem.nest_run_id == nest_run_id).all()
        part_ids_freed = set(i.part_id for i in items)
        db.query(NestRunItem).filter(NestRunItem.nest_run_id == nest_run_id).delete()

        # Delete the nest run itself
        db.delete(nr)
        db.commit()

        return {
            "success": True,
            "parts_freed": len(part_ids_freed),
            "message": f"Nest run deleted. {len(part_ids_freed)} parts unlocked for re-nesting."
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Unnest error: {str(e)}")
    finally:
        db.close()


class UnnestPartsRequest(BaseModel):
    part_ids: List[int]


@router.post("/unnest-parts")
def unnest_parts(data: UnnestPartsRequest):
    """Unnest specific parts — removes them from their nest run items."""
    db = get_db()
    try:
        freed = 0
        affected_runs = set()
        for pid in data.part_ids:
            items = db.query(NestRunItem).filter(NestRunItem.part_id == pid).all()
            for item in items:
                affected_runs.add(item.nest_run_id)
                db.delete(item)
                freed += 1

        # Update counts on affected nest runs
        for nr_id in affected_runs:
            remaining = db.query(NestRunItem).filter(NestRunItem.nest_run_id == nr_id).count()
            nr = db.query(NestRun).get(nr_id)
            if nr:
                if remaining == 0:
                    # No items left — delete the whole run and its drops
                    db.query(NestRunDrop).filter(NestRunDrop.nest_run_id == nr_id).delete()
                    db.delete(nr)
                else:
                    nr.total_parts_cut = remaining

        db.commit()
        return {"success": True, "parts_freed": freed}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Unnest error: {str(e)}")
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
    """Create an RFQ from nest run results with actual stock lengths."""
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)

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

        # Get nest items and drops to determine stock lengths per bin
        q = db.query(NestRunItem).filter(NestRunItem.nest_run_id == data.nest_run_id)
        if data.item_ids:
            q = q.filter(NestRunItem.id.in_(data.item_ids))
        nest_items = q.all()

        # Get drops to find stock lengths per stock_index
        drops = db.query(NestRunDrop).filter(NestRunDrop.nest_run_id == data.nest_run_id).all()
        stock_lengths = {}  # (shape, dims, grade, stock_index) -> stock_length_inches
        for d in drops:
            stock_lengths[(d.shape, d.dimensions, d.grade, d.stock_index)] = d.stock_length_inches

        # Consolidate by shape+dimensions+grade+stock_length
        material = {}
        for ni in nest_items:
            sl = stock_lengths.get((ni.shape, ni.dimensions, ni.grade, ni.stock_index), 480)
            sl_ft = round(sl / 12, 1)
            key = f"{ni.shape}|{ni.dimensions}|{ni.grade}|{sl_ft}"
            if key not in material:
                material[key] = {
                    "shape": ni.shape, "dimensions": ni.dimensions, "grade": ni.grade,
                    "stock_length_ft": sl_ft,
                    "stock_indices": set(),
                }
            material[key]["stock_indices"].add(ni.stock_index)

        line_num = 0
        items_created = []
        for key, mat in material.items():
            line_num += 1
            is_hw = mat["shape"] in ("HS", "NU", "WA", "MB", "ROD", "AB")
            qty = len(mat["stock_indices"])
            sl_ft = mat["stock_length_ft"]
            ft = int(sl_ft)
            inch_rem = round((sl_ft - ft) * 12)
            length_disp = f"{ft}'-{inch_rem}\"" if inch_rem else f"{ft}'-0\""

            item = RFQItemv2(
                rfq_id=rfq.id,
                line_number=line_num,
                qty=qty,
                shape=mat["shape"],
                dimensions=mat["dimensions"],
                grade=mat["grade"],
                length_display=length_disp,
                length_inches=sl_ft * 12,
                job_number=project.job_number,
                is_hardware=is_hw,
                excluded=is_hw and data.exclude_hardware,
            )
            db.add(item)
            items_created.append({
                "line": line_num, "qty": qty, "shape": mat["shape"],
                "dimensions": mat["dimensions"], "grade": mat["grade"],
                "length": length_disp, "is_hardware": is_hw,
                "excluded": is_hw and data.exclude_hardware,
            })

        db.commit()
        return {"rfq_id": rfq.id, "rfq_number": rfq_number, "items": items_created}
    finally:
        db.close()


@router.delete("/rfqs/{rfq_id}")
def delete_rfq(rfq_id: int):
    """Delete an RFQ and all associated quotes/items."""
    db = get_db()
    try:
        rfq = db.query(RFQv2).get(rfq_id)
        if not rfq:
            raise HTTPException(404)
        # Delete quote items, quotes, rfq items
        for q in db.query(RFQQuote).filter(RFQQuote.rfq_id == rfq_id).all():
            db.query(RFQQuoteItem).filter(RFQQuoteItem.quote_id == q.id).delete()
            db.delete(q)
        db.query(RFQItemv2).filter(RFQItemv2.rfq_id == rfq_id).delete()
        db.delete(rfq)
        db.commit()
        return {"success": True}
    finally:
        db.close()


@router.delete("/pos/{po_id}")
def delete_po(po_id: int):
    """Delete a PO and all line items."""
    db = get_db()
    try:
        po = db.query(POv2).get(po_id)
        if not po:
            raise HTTPException(404)
        db.query(POItemv2).filter(POItemv2.po_id == po_id).delete()
        db.delete(po)
        db.commit()
        return {"success": True}
    finally:
        db.close()


class ManualPOCreate(BaseModel):
    vendor_id: Optional[int] = None
    ordered_by: str = ""
    terms: str = "Net 45 days"
    fob: str = ""
    ship_via: str = ""
    notes: str = ""
    items: List[dict] = []  # [{qty, shape, dimensions, grade, length_display, unit_cost, unit_type}]


@router.post("/projects/{project_id}/manual-po")
def create_manual_po(project_id: int, data: ManualPOCreate):
    """Create a PO manually (self-write) without going through RFQ."""
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)

        count = db.query(POv2).filter(POv2.job_id == project_id).count()
        po_number = f"{project.job_number}-PO{count + 1:02d}"
        vendor = db.query(Vendor).get(data.vendor_id) if data.vendor_id else None

        po = POv2(
            job_id=project_id,
            po_number=po_number,
            vendor_id=data.vendor_id,
            ordered_by=data.ordered_by,
            order_date=date.today(),
            terms=data.terms or (vendor.default_terms if vendor else "Net 45 days"),
            fob=data.fob,
            ship_via=data.ship_via,
            notes=data.notes,
            status="draft",
        )
        db.add(po)
        db.flush()

        line = 0
        for item_data in data.items:
            line += 1
            barcode = generate_barcode()
            poi = POItemv2(
                po_id=po.id,
                line_number=line,
                qty=item_data.get("qty", 1),
                shape=item_data.get("shape", ""),
                dimensions=item_data.get("dimensions", ""),
                grade=item_data.get("grade", ""),
                length_display=item_data.get("length_display", ""),
                job_number=project.job_number,
                unit_cost=item_data.get("unit_cost"),
                unit_type=item_data.get("unit_type", "ea"),
                receiving_barcode=barcode,
            )
            db.add(poi)

        db.commit()
        return {"po_id": po.id, "po_number": po_number, "items": line}
    finally:
        db.close()


class HardwareRFQCreate(BaseModel):
    vendor_id: Optional[int] = None
    hw_type: str = "hardware"  # "hardware", "anchor_bolts", or "all"


@router.post("/projects/{project_id}/hardware-rfq")
def create_hardware_rfq(project_id: int, data: HardwareRFQCreate):
    """Create an RFQ from hardware items. Separates anchor bolts from other hardware."""
    db = get_db()
    try:
        project = db.query(Project).get(project_id)
        if not project:
            raise HTTPException(404)

        # Get hardware parts
        hw_q = db.query(Part).join(Assembly).filter(
            Assembly.project_id == project_id,
            Part.is_hardware == True,
        )

        if data.hw_type == "anchor_bolts":
            hw_q = hw_q.filter(or_(Part.is_anchor_bolt == True, Part.shape == "AB"))
        elif data.hw_type == "hardware":
            hw_q = hw_q.filter(or_(Part.is_anchor_bolt == False, Part.is_anchor_bolt.is_(None)))
            hw_q = hw_q.filter(Part.shape != "AB")

        hw_parts = hw_q.all()
        if not hw_parts:
            raise HTTPException(400, f"No {data.hw_type} items found")

        # Group by shape+dimensions+grade
        groups = {}
        for p in hw_parts:
            asm = db.query(Assembly).get(p.assembly_id)
            qty = p.quantity * (asm.assembly_quantity if asm else 1)
            key = f"{p.shape}|{p.dimensions}|{p.grade}"
            if key not in groups:
                groups[key] = {"shape": p.shape, "dimensions": p.dimensions, "grade": p.grade or "", "total_qty": 0}
            groups[key]["total_qty"] += qty

        count = db.query(RFQv2).filter(RFQv2.job_id == project_id).count()
        suffix = "AB" if data.hw_type == "anchor_bolts" else "HW"
        rfq_number = f"{project.job_number}-RFQ{count + 1:02d}-{suffix}"

        rfq = RFQv2(
            job_id=project_id,
            rfq_number=rfq_number,
            vendor_id=data.vendor_id,
            status="draft",
        )
        db.add(rfq)
        db.flush()

        line = 0
        items_created = []
        for key, g in groups.items():
            line += 1
            item = RFQItemv2(
                rfq_id=rfq.id,
                line_number=line,
                qty=g["total_qty"],
                shape=g["shape"],
                dimensions=g["dimensions"],
                grade=g["grade"],
                job_number=project.job_number,
                is_hardware=True,
            )
            db.add(item)
            items_created.append({"line": line, "qty": g["total_qty"], "shape": g["shape"], "dimensions": g["dimensions"]})

        db.commit()
        return {"rfq_id": rfq.id, "rfq_number": rfq_number, "items": items_created, "hw_type": data.hw_type}
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
            items = db.query(RFQItemv2).filter(RFQItemv2.rfq_id == r.id, or_(RFQItemv2.excluded == False, RFQItemv2.excluded.is_(None))).all()
            result.append({
                "id": r.id,
                "rfq_number": r.rfq_number,
                "vendor_name": vendor.name if vendor else "Unassigned",
                "vendor_id": r.vendor_id,
                "status": r.status,
                "item_count": len(items),
                "quote_count": db.query(RFQQuote).filter(RFQQuote.rfq_id == r.id).count(),
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
#  RFQ QUOTE COMPARISON
# ═══════════════════════════════════════════════════════════════

@router.get("/rfqs/{rfq_id}/quotes")
def list_rfq_quotes(rfq_id: int):
    """List all vendor quotes for an RFQ."""
    db = get_db()
    try:
        quotes = db.query(RFQQuote).filter(RFQQuote.rfq_id == rfq_id).order_by(RFQQuote.total_price).all()
        return [{
            "id": q.id,
            "vendor_id": q.vendor_id,
            "vendor_name": q.vendor.name if q.vendor else "Unknown",
            "quote_date": q.quote_date.isoformat() if q.quote_date else None,
            "expiry_date": q.expiry_date.isoformat() if q.expiry_date else None,
            "sub_total": float(q.sub_total or 0),
            "tax": float(q.tax or 0),
            "freight": float(q.freight or 0),
            "total_price": float(q.total_price or 0),
            "lead_time_days": q.lead_time_days,
            "terms": q.terms,
            "notes": q.notes,
            "quote_filename": q.quote_filename,
            "has_pdf": bool(q.quote_pdf),
            "is_selected": q.is_selected,
            "line_items": [{
                "id": li.id, "line_number": li.line_number,
                "description": li.description, "qty": li.qty,
                "unit_price": float(li.unit_price or 0), "unit_type": li.unit_type,
                "total_price": float(li.total_price or 0),
            } for li in (q.line_items or [])],
        } for q in quotes]
    finally:
        db.close()


@router.post("/rfqs/{rfq_id}/quotes")
async def upload_rfq_quote(
    rfq_id: int,
    vendor_id: int = Form(...),
    sub_total: float = Form(0),
    tax: float = Form(0),
    freight: float = Form(0),
    total_price: float = Form(0),
    lead_time_days: int = Form(0),
    terms: str = Form(""),
    notes: str = Form(""),
    quote_file: Optional[UploadFile] = File(None),
):
    """Upload a vendor quote against an RFQ."""
    db = get_db()
    try:
        rfq = db.query(RFQv2).get(rfq_id)
        if not rfq:
            raise HTTPException(404, "RFQ not found")

        import base64 as b64mod
        pdf_data = None
        filename = None
        if quote_file and quote_file.filename:
            content = await quote_file.read()
            pdf_data = b64mod.b64encode(content).decode('utf-8')
            filename = quote_file.filename

        q = RFQQuote(
            rfq_id=rfq_id,
            vendor_id=vendor_id,
            quote_date=date.today(),
            sub_total=sub_total,
            tax=tax,
            freight=freight,
            total_price=total_price if total_price > 0 else sub_total + tax + freight,
            lead_time_days=lead_time_days,
            terms=terms,
            notes=notes,
            quote_pdf=pdf_data,
            quote_filename=filename,
        )
        db.add(q)
        db.commit()
        db.refresh(q)

        # Auto-update RFQ status
        rfq.status = "received"
        db.commit()

        return {"id": q.id, "vendor_name": q.vendor.name if q.vendor else ""}
    finally:
        db.close()


@router.put("/rfq-quotes/{quote_id}/select")
def select_rfq_quote(quote_id: int):
    """Select a quote as the winner (deselects others on same RFQ)."""
    db = get_db()
    try:
        q = db.query(RFQQuote).get(quote_id)
        if not q:
            raise HTTPException(404)
        # Deselect all other quotes on this RFQ
        db.query(RFQQuote).filter(RFQQuote.rfq_id == q.rfq_id).update({"is_selected": False})
        q.is_selected = True
        # Update RFQ with winning vendor and pricing
        rfq = db.query(RFQv2).get(q.rfq_id)
        if rfq:
            rfq.vendor_id = q.vendor_id
            rfq.sub_total = q.sub_total
            rfq.tax = q.tax
            rfq.freight = q.freight
            rfq.total_price = q.total_price
            rfq.status = "accepted"
        db.commit()
        return {"success": True}
    finally:
        db.close()


@router.get("/rfq-quotes/{quote_id}/pdf")
def get_rfq_quote_pdf(quote_id: int):
    db = get_db()
    try:
        q = db.query(RFQQuote).get(quote_id)
        if not q or not q.quote_pdf:
            raise HTTPException(404)
        return {"pdf_data": q.quote_pdf, "filename": q.quote_filename}
    finally:
        db.close()


@router.get("/rfqs/{rfq_id}/comparison")
def get_rfq_comparison(rfq_id: int):
    """Get a comparison summary of all quotes for an RFQ."""
    db = get_db()
    try:
        rfq = db.query(RFQv2).get(rfq_id)
        if not rfq:
            raise HTTPException(404)
        quotes = db.query(RFQQuote).filter(RFQQuote.rfq_id == rfq_id).order_by(RFQQuote.total_price).all()
        if not quotes:
            return {"rfq_id": rfq_id, "quotes": [], "recommendation": None}

        lowest = quotes[0]
        return {
            "rfq_id": rfq_id,
            "rfq_number": rfq.rfq_number,
            "quotes": [{
                "id": q.id,
                "vendor_name": q.vendor.name if q.vendor else "Unknown",
                "total_price": float(q.total_price or 0),
                "freight": float(q.freight or 0),
                "lead_time_days": q.lead_time_days,
                "terms": q.terms,
                "is_selected": q.is_selected,
                "savings_vs_highest": float((quotes[-1].total_price or 0) - (q.total_price or 0)) if len(quotes) > 1 else 0,
            } for q in quotes],
            "recommendation": {
                "vendor_name": lowest.vendor.name if lowest.vendor else "Unknown",
                "total_price": float(lowest.total_price or 0),
                "reason": f"Lowest total price" + (f" with {lowest.lead_time_days} day lead time" if lowest.lead_time_days else ""),
            }
        }
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
            or_(RFQItemv2.excluded == False, RFQItemv2.excluded.is_(None)),
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


@router.get("/projects/{project_id}/hardware-summary")
def get_hardware_summary(project_id: int):
    """Get a summary of all hardware items, split by anchor bolts vs other hardware."""
    db = get_db()
    try:
        hw_parts = db.query(Part).join(Assembly).filter(
            Assembly.project_id == project_id,
            Part.is_hardware == True,
        ).all()

        groups = {}
        for p in hw_parts:
            asm = db.query(Assembly).get(p.assembly_id)
            qty = p.quantity * (asm.assembly_quantity if asm else 1)
            is_ab = getattr(p, 'is_anchor_bolt', False) or p.shape == 'AB'
            key = f"{p.shape}|{p.dimensions}|{p.grade}|{'AB' if is_ab else 'HW'}"
            if key not in groups:
                groups[key] = {
                    "shape": p.shape, "dimensions": p.dimensions, "grade": p.grade or "",
                    "length_display": p.length_display or "", "total_qty": 0,
                    "is_anchor_bolt": is_ab, "parts": [],
                }
            groups[key]["total_qty"] += qty
            groups[key]["parts"].append({
                "part_mark": p.part_mark,
                "assembly_mark": asm.assembly_mark if asm else "",
                "qty": qty,
            })
        return list(groups.values())
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


@router.get("/packet-attachments/{attachment_id}/download")
def download_packet_attachment(attachment_id: int):
    """Download a single packet attachment."""
    db = get_db()
    try:
        a = db.query(PacketAttachment).get(attachment_id)
        if not a or not a.file_data:
            raise HTTPException(404)
        return {"file_data": a.file_data, "filename": a.filename}
    finally:
        db.close()


@router.delete("/packet-attachments/{attachment_id}")
def delete_packet_attachment(attachment_id: int):
    """Delete a packet attachment and update count."""
    db = get_db()
    try:
        a = db.query(PacketAttachment).get(attachment_id)
        if not a:
            raise HTTPException(404)
        packet = db.query(DocumentPacket).get(a.packet_id)
        if packet:
            packet.attachment_count = max((packet.attachment_count or 1) - 1, 0)
        db.delete(a)
        db.commit()
        return {"success": True}
    finally:
        db.close()


@router.post("/packets/{packet_id}/attach-drawings")
def attach_drawings_to_packet(
    packet_id: int,
    drawing_ids: str = Form(""),
):
    """Attach project drawings to a transmittal packet."""
    import base64 as b64mod
    db = get_db()
    try:
        packet = db.query(DocumentPacket).get(packet_id)
        if not packet:
            raise HTTPException(404)

        ids = [int(x.strip()) for x in drawing_ids.split(",") if x.strip()]
        attached = 0

        for did in ids:
            dwg = db.query(Drawing).get(did)
            if not dwg:
                continue
            # Skip if already attached
            existing = db.query(PacketAttachment).filter(
                PacketAttachment.packet_id == packet_id,
                PacketAttachment.filename == f"{dwg.drawing_number}_Rev{dwg.current_revision or '0'}.pdf",
            ).first()
            if existing:
                continue

            att = PacketAttachment(
                packet_id=packet_id,
                filename=f"{dwg.drawing_number}_Rev{dwg.current_revision or '0'}.pdf",
                file_data=dwg.pdf_data if dwg.pdf_data else None,
                file_size=len(dwg.pdf_data or "") * 3 // 4,
                sort_order=(packet.attachment_count or 0) + attached,
            )
            db.add(att)
            attached += 1

        packet.attachment_count = (packet.attachment_count or 0) + attached
        db.commit()
        return {"attached": attached, "total": packet.attachment_count}
    finally:
        db.close()


@router.get("/packets/{packet_id}/download-zip")
def download_packet_zip(packet_id: int):
    """Download all packet attachments as a zip (base64 encoded)."""
    import zipfile as zf
    import base64 as b64mod

    db = get_db()
    try:
        packet = db.query(DocumentPacket).get(packet_id)
        if not packet:
            raise HTTPException(404)
        atts = db.query(PacketAttachment).filter(
            PacketAttachment.packet_id == packet_id,
        ).order_by(PacketAttachment.sort_order).all()

        if not atts:
            raise HTTPException(404, "No attachments")

        buf = io.BytesIO()
        with zf.ZipFile(buf, 'w', zf.ZIP_DEFLATED) as z:
            for a in atts:
                if a.file_data:
                    z.writestr(a.filename or f"file_{a.id}", b64mod.b64decode(a.file_data))
        buf.seek(0)

        zip_b64 = b64mod.b64encode(buf.getvalue()).decode('utf-8')
        return {
            "zip_data": zip_b64,
            "filename": f"{packet.doc_number or 'document'}.zip",
        }
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  STOCK SIZE LIBRARY
# ═══════════════════════════════════════════════════════════════

@router.get("/stock-config")
def list_stock_config():
    db = get_db()
    try:
        configs = db.query(StockConfig).filter(StockConfig.active == True).order_by(StockConfig.shape_code).all()
        return [{
            "id": c.id,
            "shape_code": c.shape_code,
            "nest_type": c.nest_type,
            "available_lengths": c.available_lengths,
            "kerf_inches": c.kerf_inches,
            "notes": c.notes,
        } for c in configs]
    finally:
        db.close()


@router.get("/stock-config/for-shape/{shape_code}")
def get_stock_for_shape(shape_code: str):
    """Get available stock lengths for a given shape code."""
    db = get_db()
    try:
        # Map shape codes to stock config
        shape_map = {
            "W": "W", "WF": "W",
            "HSS": "HSS", "TS": "HSS",
            "HSSR": "HSSR",
            "RT": "RT",
            "PIPE": "PIPE",
            "L": "L",
            "C": "C",
            "MC": "MC",
            "S": "S",
            "PL": "PL", "PLATE": "PL",
        }
        mapped = shape_map.get(shape_code.upper(), shape_code.upper())
        config = db.query(StockConfig).filter(
            StockConfig.shape_code == mapped,
            StockConfig.active == True
        ).first()
        if not config:
            # Fallback - return generic lengths
            return {"shape_code": mapped, "nest_type": "mult", "available_lengths": [20, 40], "kerf_inches": 0.125}
        return {
            "shape_code": config.shape_code,
            "nest_type": config.nest_type,
            "available_lengths": config.available_lengths,
            "kerf_inches": config.kerf_inches,
        }
    finally:
        db.close()


class StockConfigUpdate(BaseModel):
    shape_code: str
    nest_type: str = "mult"
    available_lengths: list = []
    kerf_inches: float = 0.125
    notes: str = ""

@router.post("/stock-config")
def create_stock_config(data: StockConfigUpdate):
    db = get_db()
    try:
        c = StockConfig(**data.model_dump())
        db.add(c)
        db.commit()
        db.refresh(c)
        return {"id": c.id, "shape_code": c.shape_code}
    finally:
        db.close()

@router.put("/stock-config/{config_id}")
def update_stock_config(config_id: int, data: StockConfigUpdate):
    db = get_db()
    try:
        c = db.query(StockConfig).get(config_id)
        if not c:
            raise HTTPException(404)
        for k, v in data.model_dump().items():
            setattr(c, k, v)
        db.commit()
        return {"success": True}
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  ASSEMBLY-LEVEL UPDATES
# ═══════════════════════════════════════════════════════════════

@router.put("/assemblies/{assembly_id}/finish")
def update_assembly_finish(assembly_id: int, finish_type: str = Form(...)):
    """Update finish type on an individual assembly."""
    db = get_db()
    try:
        a = db.query(Assembly).get(assembly_id)
        if not a:
            raise HTTPException(404)
        a.finish_type = finish_type
        db.commit()
        return {"success": True, "finish_type": finish_type}
    finally:
        db.close()
