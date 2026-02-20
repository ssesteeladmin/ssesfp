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
from fastapi import APIRouter, HTTPException, Form, Query, UploadFile, File, Body
from pydantic import BaseModel
import httpx
from sqlalchemy import desc, func, or_, text
from sqlalchemy.orm import Session

from models import (
    Base, Project, Assembly, Part, Drawing, ScanEvent, ChangeOrder
)
from models_phase25 import (
    Vendor, NestRun, NestRunItem, NestRunDrop,
    RFQv2, RFQItemv2, POv2, POItemv2,
    RFQQuote, RFQQuoteItem,
    YardTag, DropTag, MaterialInventory,
    DocumentPacket, PacketAttachment, StockConfig,
    ProductionFolder, ProductionFolderItem,
    SOVLine, Invoice, InvoiceLineItem,
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
    stock_overrides: dict = {}  # {"HSS": [20, 24], "PIPE": [21]} - override per shape (feet)
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
        # For PLATES: group by thickness+grade (combine all widths onto shared sheets)
        PLATE_SHAPES = ("PL", "PLATE")
        groups = {}
        for p in parts_to_nest:
            if p['shape'].upper() in PLATE_SHAPES:
                # Parse thickness from dimensions (e.g. "1/2\"X12\"" → thickness="1/2")
                thickness_str = _extract_plate_thickness_str(p['dimensions'])
                key = f"{p['shape']}|{thickness_str}|{p['grade']}"
                if key not in groups:
                    groups[key] = {"shape": p['shape'], "dimensions": thickness_str, "grade": p['grade'], "items": [], "is_plate": True}
                # Parse the plate piece width from dimensions (part after X)
                piece_w = _extract_plate_width(p['dimensions'])
                if piece_w and not p.get('width_inches'):
                    p['width_inches'] = piece_w
                groups[key]["items"].append(p)
            else:
                key = f"{p['shape']}|{p['dimensions']}|{p['grade']}"
                if key not in groups:
                    groups[key] = {"shape": p['shape'], "dimensions": p['dimensions'], "grade": p['grade'], "items": [], "is_plate": False}
                groups[key]["items"].append(p)

        # Shape to stock config mapping
        shape_map = {"W": "W", "WF": "W", "HSS": "HSS", "TS": "HSS", "HSSR": "HSSR", "RT": "RT",
                     "PIPE": "PIPE", "L": "L", "C": "C", "MC": "MC", "S": "S", "PL": "PL", "PLATE": "PL"}

        all_results = []
        warnings = []
        total_used = 0
        total_material = 0
        buy_list = []  # consolidated purchase list

        for key, group in groups.items():
            is_plate = group.get("is_plate", False)

            # Get stock config
            mapped_shape = shape_map.get(group["shape"].upper(), group["shape"].upper())
            stock_cfg = db.query(StockConfig).filter(
                StockConfig.shape_code == mapped_shape,
                StockConfig.active == True
            ).first()
            kerf = stock_cfg.kerf_inches if stock_cfg else 0.125

            if is_plate and data.nest_mode != "mult":
                # ═══ PLATE NESTING (2D strip packing by thickness) ═══
                plates = []
                part_refs = []
                for item in group["items"]:
                    for _ in range(item["quantity"]):
                        w = item.get("width_inches") or _extract_plate_width(item.get("dimensions", "")) or 12
                        l = item["length_inches"] or 12
                        plates.append({"width": w, "length": l, "part_dims": item["dimensions"]})
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

                    # Sort plates by area descending (largest first)
                    sorted_p = sorted(enumerate(plates), key=lambda x: -(x[1]["width"] * x[1]["length"]))
                    sheets_used = []
                    cur = {"strips": [], "y_used": 0}

                    for orig_idx, pl in sorted_p:
                        pw, pl_len = pl["width"], pl["length"]
                        placed = False

                        # Try both orientations
                        orientations = [(pw, pl_len), (pl_len, pw)]
                        for ow, ol in orientations:
                            if ow > sw or ol > sl:
                                continue  # doesn't fit this orientation

                            # Try existing strips on current sheet
                            for strip in cur["strips"]:
                                if strip["x_rem"] >= ol + kerf and strip["h"] >= ow:
                                    strip["cuts"].append((orig_idx, ow, ol))
                                    strip["x_rem"] -= (ol + kerf)
                                    placed = True
                                    break
                            if placed:
                                break

                            # New strip on current sheet
                            if not placed and cur["y_used"] + ow + kerf <= sw:
                                cur["strips"].append({
                                    "h": ow, "x_rem": sl - ol - kerf,
                                    "cuts": [(orig_idx, ow, ol)]
                                })
                                cur["y_used"] += ow + kerf
                                placed = True
                                break

                        if not placed:
                            # New sheet — try both orientations again
                            for ow, ol in orientations:
                                if ow <= sw and ol <= sl:
                                    sheets_used.append(cur)
                                    cur = {
                                        "strips": [{"h": ow, "x_rem": sl - ol - kerf, "cuts": [(orig_idx, ow, ol)]}],
                                        "y_used": ow + kerf,
                                    }
                                    placed = True
                                    break
                            if not placed:
                                # Piece too large for any sheet — put on its own
                                sheets_used.append(cur)
                                cur = {
                                    "strips": [{"h": pw, "x_rem": 0, "cuts": [(orig_idx, pw, pl_len)]}],
                                    "y_used": pw,
                                }

                    sheets_used.append(cur)
                    # Filter out empty sheets
                    sheets_used = [s for s in sheets_used if s["strips"] and any(strip["cuts"] for strip in s["strips"])]

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
                    "stock_desc": f"{sheet['w']}'×{sheet['l']}' PL {group['dimensions']}\"",
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
                shape_upper = (group["shape"] or "").upper()
                if shape_upper in data.stock_overrides and data.stock_overrides[shape_upper]:
                    # Manual override for this specific shape
                    avail_inches = sorted([l * 12 for l in data.stock_overrides[shape_upper]])
                elif data.stock_length_inches > 0:
                    avail_inches = [data.stock_length_inches]
                elif stock_cfg and stock_cfg.available_lengths:
                    avail_inches = sorted([l * 12 for l in stock_cfg.available_lengths])
                else:
                    avail_inches = [480]  # 40ft default

                max_stock = max(avail_inches)

                # ⚠️ Check for oversized parts
                for ci, cl in enumerate(cut_lengths):
                    if cl > max_stock:
                        warnings.append({
                            "type": "oversized",
                            "shape": group["shape"],
                            "dimensions": group["dimensions"],
                            "part_mark": part_refs[ci].get("part_mark", ""),
                            "part_length_ft": round(cl / 12, 1),
                            "max_stock_ft": round(max_stock / 12, 1),
                            "message": f"⚠️ {group['shape']} {group['dimensions']} part {part_refs[ci].get('part_mark', '')} is {round(cl / 12, 1)}' — exceeds max stock length of {round(max_stock / 12, 1)}'",
                        })

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
                        shape=p_ref["shape"],
                        dimensions=p_ref["dimensions"],
                        grade=p_ref["grade"],
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
                "dimensions": (r["dimensions"] + '"') if r.get("is_plate") else r["dimensions"],
                "grade": r["grade"],
                "is_plate": r.get("is_plate", False),
                "stock_count": len(r["bins"]),
                "yield_pct": r.get("yield_pct"),
                "sheet_desc": r.get("sheet_desc"),
                # For mult: show mixed lengths used
                "stock_lengths": list(set(round(b["stock_length"] / 12, 1) for b in r["bins"])) if not r.get("is_plate") else [],
            } for r in all_results],
            "warnings": warnings,
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
    s = _extract_plate_thickness_str(dimensions_str)
    return _parse_mixed_fraction(s) or 0.5


def _extract_plate_thickness_str(dimensions_str):
    """Extract thickness portion from plate dimensions like '1/2\"X12\"' → '1/2'."""
    s = (dimensions_str or "").strip().replace('"', '').replace("'", "")
    # Split on X (case-insensitive)
    import re
    parts = re.split(r'[xX]', s, maxsplit=1)
    return parts[0].strip() if parts else s


def _extract_plate_width(dimensions_str):
    """Extract width (piece width) from plate dimensions like '1/2\"X12\"' → 12.0 inches."""
    s = (dimensions_str or "").strip().replace('"', '').replace("'", "")
    import re
    parts = re.split(r'[xX]', s, maxsplit=1)
    if len(parts) < 2:
        return 0
    w_str = parts[1].strip()
    return _parse_mixed_fraction(w_str)


def _parse_mixed_fraction(s):
    """Parse strings like '6 1/8', '1-1/2', '14 5/8', '3/4', '12' into float."""
    s = s.strip()
    if not s:
        return 0
    try:
        # Try space-separated mixed: "6 1/8" or "14 5/8"
        import re
        m = re.match(r'^(\d+)\s+(\d+)/(\d+)$', s)
        if m:
            return float(m.group(1)) + float(m.group(2)) / float(m.group(3))
        # Try dash-separated mixed: "1-1/4"
        m = re.match(r'^(\d+)-(\d+)/(\d+)$', s)
        if m:
            return float(m.group(1)) + float(m.group(2)) / float(m.group(3))
        # Try simple fraction: "3/4"
        if "/" in s:
            num, den = s.split("/")
            return float(num) / float(den)
        # Plain number
        return float(s)
    except (ValueError, ZeroDivisionError):
        return 0


def _generate_inv_barcode():
    """Generate inventory-specific barcode with INV- prefix."""
    return f"INV-{uuid.uuid4().hex[:8].upper()}"


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

        # Nullify any RFQ references to this nest run
        db.query(RFQv2).filter(RFQv2.nest_run_id == nest_run_id).update(
            {RFQv2.nest_run_id: None}, synchronize_session=False
        )

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
                    db.query(RFQv2).filter(RFQv2.nest_run_id == nr_id).update(
                        {RFQv2.nest_run_id: None}, synchronize_session=False
                    )
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
        vid = data.vendor_id if data.vendor_id and data.vendor_id > 0 else None
        vendor = db.query(Vendor).get(vid) if vid else None

        po = POv2(
            job_id=project_id,
            po_number=po_number,
            vendor_id=vid,
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
            raw_cost = item_data.get("unit_cost")
            poi = POItemv2(
                po_id=po.id,
                line_number=line,
                qty=int(item_data.get("qty") or 1),
                shape=item_data.get("shape", ""),
                dimensions=item_data.get("dimensions", ""),
                grade=item_data.get("grade", ""),
                length_display=item_data.get("length_display", ""),
                job_number=project.job_number,
                unit_cost=float(raw_cost) if raw_cost and str(raw_cost).strip() else None,
                unit_type=item_data.get("unit_type", "ea"),
                receiving_barcode=barcode,
            )
            db.add(poi)

        db.commit()
        
        # Push to production receiving dashboard
        try:
            vendor_name = vendor.name if vendor else 'Unknown'
            xc = httpx.Client(timeout=10)
            xc.post("https://ssesteeldashboard.up.railway.app/api/v1/receiving", json={
                "job_number": project.job_number,
                "po_number": po_number,
                "vendor": vendor_name,
                "pm": data.ordered_by or "",
                "description": "PO from " + vendor_name + " - " + str(line) + " items",
                "status": "open",
                "source": "ssesfp",
                "created_by": "ssesfp"
            })
            xc.close()
            print('Receiving: pushed ' + po_number)
        except Exception as rx:
            print('Receiving push failed: ' + str(rx))
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
def list_material_inventory(
    shape: Optional[str] = None,
    status: str = "available",
    search: Optional[str] = None,
    grade: Optional[str] = None,
):
    """List inventory with search/filter support."""
    db = get_db()
    try:
        q = db.query(MaterialInventory)
        if status and status != "all":
            q = q.filter(MaterialInventory.status == status)
        if shape:
            q = q.filter(MaterialInventory.shape.ilike(f"%{shape}%"))
        if grade:
            q = q.filter(MaterialInventory.grade.ilike(f"%{grade}%"))
        if search:
            term = f"%{search}%"
            q = q.filter(
                (MaterialInventory.member_size.ilike(term)) |
                (MaterialInventory.shape.ilike(term)) |
                (MaterialInventory.dimensions.ilike(term)) |
                (MaterialInventory.location.ilike(term)) |
                (MaterialInventory.heat_number.ilike(term)) |
                (MaterialInventory.barcode.ilike(term)) |
                (MaterialInventory.notes.ilike(term))
            )
        items = q.order_by(MaterialInventory.shape, MaterialInventory.member_size, MaterialInventory.length_inches.desc()).all()
        return [_inv_to_dict(i, db) for i in items]
    finally:
        db.close()


def _inv_to_dict(i, db=None):
    proj_name = None
    if i.reserved_project_id and db:
        proj = db.query(Project).get(i.reserved_project_id)
        if proj:
            proj_name = f"{proj.job_number} - {proj.name}"
    return {
        "id": i.id,
        "barcode": i.barcode,
        "source_type": i.source_type,
        "member_size": i.member_size,
        "shape": i.shape,
        "dimensions": i.dimensions,
        "length_display": i.length_display,
        "length_inches": float(i.length_inches) if i.length_inches else 0,
        "width_inches": float(i.width_inches) if i.width_inches else None,
        "quantity": i.quantity or 1,
        "weight": float(i.weight) if i.weight else 0,
        "grade": i.grade,
        "heat_number": i.heat_number,
        "location": i.location,
        "status": i.status,
        "reserved_project_id": i.reserved_project_id,
        "reserved_project": proj_name,
        "reserved_by": i.reserved_by,
        "reserved_date": i.reserved_date.isoformat() if i.reserved_date else None,
        "added_date": i.added_date.isoformat() if i.added_date else None,
        "added_by": i.added_by,
        "notes": i.notes,
    }


@router.get("/inventory-v2/summary")
def inventory_summary():
    """Get counts by shape for dashboard."""
    db = get_db()
    try:
        items = db.query(MaterialInventory).filter(MaterialInventory.status == "available").all()
        shapes = {}
        for i in items:
            s = i.shape or "Other"
            if s not in shapes:
                shapes[s] = {"count": 0, "total_qty": 0}
            shapes[s]["count"] += 1
            shapes[s]["total_qty"] += (i.quantity or 1)
        return {"shapes": shapes, "total_items": len(items)}
    finally:
        db.close()


class InventoryItemCreate(BaseModel):
    shape: str
    dimensions: str
    grade: str = "A36"
    length_display: str = ""
    length_inches: float = 0
    width_inches: Optional[float] = None
    quantity: int = 1
    weight: Optional[float] = None
    heat_number: str = ""
    location: str = ""
    source_type: str = "manual"
    notes: str = ""
    added_by: str = ""


@router.post("/inventory-v2")
def add_inventory_item(data: InventoryItemCreate):
    """Add a single inventory item and generate barcode."""
    db = get_db()
    try:
        barcode = _generate_inv_barcode()
        member_size = f"{data.shape} {data.dimensions}" if data.dimensions else data.shape
        inv = MaterialInventory(
            barcode=barcode,
            source_type=data.source_type or "manual",
            member_size=member_size,
            shape=data.shape.upper(),
            dimensions=data.dimensions,
            length_display=data.length_display or (f"{round(data.length_inches / 12, 1)}'" if data.length_inches else ""),
            length_inches=data.length_inches,
            width_inches=data.width_inches,
            quantity=data.quantity or 1,
            weight=data.weight,
            grade=data.grade or "A36",
            heat_number=data.heat_number,
            location=data.location,
            status="available",
            added_by=data.added_by,
            notes=data.notes,
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        return _inv_to_dict(inv)
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Error: {str(e)}")
    finally:
        db.close()


@router.post("/inventory-v2/bulk-csv")
async def bulk_add_inventory_csv(file: UploadFile = File(...), added_by: str = Form("")):
    """Bulk import inventory from CSV. Columns: shape, dimensions, grade, length_display, length_inches, width_inches, quantity, heat_number, location, notes"""
    import csv, io
    db = get_db()
    try:
        content = (await file.read()).decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        added = 0
        errors = []
        for row_num, row in enumerate(reader, start=2):
            try:
                # Normalize column names (strip whitespace, lowercase)
                row = {k.strip().lower().replace(" ", "_"): v.strip() for k, v in row.items() if k}
                shape = row.get("shape", "").upper()
                dims = row.get("dimensions", "")
                if not shape:
                    errors.append(f"Row {row_num}: missing shape")
                    continue

                length_in = 0
                ld = row.get("length_display", "") or row.get("length", "")
                li = row.get("length_inches", "")
                if li:
                    try:
                        length_in = float(li)
                    except ValueError:
                        pass
                elif ld:
                    # Try parsing "20'" or "240" from display
                    ld_clean = ld.replace("'", "").replace('"', "").strip()
                    try:
                        val = float(ld_clean)
                        length_in = val * 12 if val < 100 else val  # assume feet if < 100
                    except ValueError:
                        pass

                qty = 1
                try:
                    qty = int(row.get("quantity", "1") or "1")
                except ValueError:
                    pass

                barcode = _generate_inv_barcode()
                inv = MaterialInventory(
                    barcode=barcode,
                    source_type="manual",
                    member_size=f"{shape} {dims}" if dims else shape,
                    shape=shape,
                    dimensions=dims,
                    length_display=ld or (f"{round(length_in / 12, 1)}'" if length_in else ""),
                    length_inches=length_in,
                    width_inches=float(row.get("width_inches", "0") or "0") or None,
                    quantity=qty,
                    weight=float(row.get("weight", "0") or "0") or None,
                    grade=row.get("grade", "A36") or "A36",
                    heat_number=row.get("heat_number", ""),
                    location=row.get("location", ""),
                    status="available",
                    added_by=added_by,
                    notes=row.get("notes", ""),
                )
                db.add(inv)
                added += 1
            except Exception as e:
                errors.append(f"Row {row_num}: {str(e)}")

        db.commit()
        return {"success": True, "added": added, "errors": errors}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"CSV import error: {str(e)}")
    finally:
        db.close()


@router.put("/inventory-v2/{item_id}")
def update_inventory_item(item_id: int, data: InventoryItemCreate):
    """Update an inventory item."""
    db = get_db()
    try:
        inv = db.query(MaterialInventory).get(item_id)
        if not inv:
            raise HTTPException(404, "Item not found")
        inv.shape = data.shape.upper()
        inv.dimensions = data.dimensions
        inv.member_size = f"{data.shape} {data.dimensions}" if data.dimensions else data.shape
        inv.grade = data.grade or "A36"
        inv.length_display = data.length_display
        inv.length_inches = data.length_inches
        inv.width_inches = data.width_inches
        inv.quantity = data.quantity or 1
        inv.weight = data.weight
        inv.heat_number = data.heat_number
        inv.location = data.location
        inv.notes = data.notes
        if not inv.barcode:
            inv.barcode = _generate_inv_barcode()
        db.commit()
        db.refresh(inv)
        return _inv_to_dict(inv)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.delete("/inventory-v2/{item_id}")
def delete_inventory_item(item_id: int):
    db = get_db()
    try:
        inv = db.query(MaterialInventory).get(item_id)
        if not inv:
            raise HTTPException(404, "Item not found")
        db.delete(inv)
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/inventory-v2/{item_id}/reserve")
def reserve_inventory_item(
    item_id: int,
    project_id: int = Form(...),
    reserved_by: str = Form(""),
    qty_to_reserve: int = Form(0),
):
    """Reserve inventory for a project. If qty_to_reserve < total qty, splits the item."""
    db = get_db()
    try:
        inv = db.query(MaterialInventory).get(item_id)
        if not inv:
            raise HTTPException(404, "Item not found")
        if inv.status != "available":
            raise HTTPException(400, f"Item is {inv.status}, not available")

        total_qty = inv.quantity or 1
        reserve_qty = qty_to_reserve if qty_to_reserve > 0 else total_qty

        if reserve_qty > total_qty:
            raise HTTPException(400, f"Only {total_qty} available")

        if reserve_qty < total_qty:
            # Split: reduce original qty, create new reserved item
            inv.quantity = total_qty - reserve_qty
            new_inv = MaterialInventory(
                barcode=_generate_inv_barcode(),
                source_type=inv.source_type,
                member_size=inv.member_size,
                shape=inv.shape,
                dimensions=inv.dimensions,
                length_display=inv.length_display,
                length_inches=inv.length_inches,
                width_inches=inv.width_inches,
                quantity=reserve_qty,
                weight=inv.weight,
                grade=inv.grade,
                heat_number=inv.heat_number,
                location=inv.location,
                status="reserved",
                reserved_project_id=project_id,
                reserved_date=datetime.utcnow(),
                reserved_by=reserved_by,
                added_date=inv.added_date,
                added_by=inv.added_by,
                notes=inv.notes,
            )
            db.add(new_inv)
        else:
            # Reserve the entire item
            inv.status = "reserved"
            inv.reserved_project_id = project_id
            inv.reserved_date = datetime.utcnow()
            inv.reserved_by = reserved_by

        db.commit()
        return {"success": True, "reserved_qty": reserve_qty}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/inventory-v2/{item_id}/release")
def release_inventory_item(item_id: int):
    """Release a reserved item back to available."""
    db = get_db()
    try:
        inv = db.query(MaterialInventory).get(item_id)
        if not inv:
            raise HTTPException(404, "Item not found")
        inv.status = "available"
        inv.reserved_project_id = None
        inv.reserved_date = None
        inv.reserved_by = None
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/inventory-v2/{item_id}/update-location")
def update_inventory_location(item_id: int, location: str = Form(...)):
    """Update location after scanning."""
    db = get_db()
    try:
        inv = db.query(MaterialInventory).get(item_id)
        if not inv:
            raise HTTPException(404, "Item not found")
        inv.location = location
        db.commit()
        return {"success": True, "location": location}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.get("/inventory-v2/barcode/{barcode}")
def lookup_inventory_by_barcode(barcode: str):
    """Look up inventory item by barcode."""
    db = get_db()
    try:
        inv = db.query(MaterialInventory).filter(MaterialInventory.barcode == barcode.upper()).first()
        if not inv:
            raise HTTPException(404, "Inventory item not found")
        return _inv_to_dict(inv, db)
    finally:
        db.close()


@router.post("/projects/{project_id}/inventory-check")
def check_inventory_before_rfq(project_id: int):
    """
    Compare the nesting buy list against available inventory.
    Returns matches so PM can decide what to pull from stock vs buy.
    """
    db = get_db()
    try:
        # Get the latest nest run for this project
        latest_run = db.query(NestRun).filter(NestRun.job_id == project_id).order_by(desc(NestRun.nest_date)).first()
        if not latest_run:
            return {"matches": [], "message": "No nest run found. Run nesting first."}

        # Get nest items grouped by shape+dimensions+grade+stock_length
        nest_items = db.query(NestRunItem).filter(NestRunItem.nest_run_id == latest_run.id).all()

        # Build buy list from nest items (group by material)
        buy_groups = {}
        for ni in nest_items:
            key = f"{(ni.shape or '').upper()}|{ni.dimensions or ''}|{ni.grade or ''}"
            if key not in buy_groups:
                buy_groups[key] = {"shape": ni.shape, "dimensions": ni.dimensions, "grade": ni.grade, "needed_qty": 0}
            buy_groups[key]["needed_qty"] += 1

        # Search inventory for matches
        available = db.query(MaterialInventory).filter(
            MaterialInventory.status == "available",
            MaterialInventory.quantity > 0,
        ).all()

        matches = []
        for key, need in buy_groups.items():
            shape = (need["shape"] or "").upper()
            dims = (need["dimensions"] or "").strip()
            grade = (need["grade"] or "").strip()

            matching_inv = []
            for inv in available:
                inv_shape = (inv.shape or "").upper()
                inv_dims = (inv.dimensions or "").strip()
                inv_grade = (inv.grade or "").strip()

                # Match on shape + dimensions + grade
                if inv_shape == shape and inv_dims == dims and inv_grade == grade:
                    matching_inv.append({
                        "inv_id": inv.id,
                        "barcode": inv.barcode,
                        "length_display": inv.length_display,
                        "length_inches": float(inv.length_inches) if inv.length_inches else 0,
                        "quantity": inv.quantity or 1,
                        "location": inv.location,
                        "heat_number": inv.heat_number,
                    })

                # Also match on member_size for broader catch
                elif inv.member_size and inv.member_size.upper() == f"{shape} {dims}".upper():
                    if not inv_grade or inv_grade.upper() == grade.upper():
                        matching_inv.append({
                            "inv_id": inv.id,
                            "barcode": inv.barcode,
                            "length_display": inv.length_display,
                            "length_inches": float(inv.length_inches) if inv.length_inches else 0,
                            "quantity": inv.quantity or 1,
                            "location": inv.location,
                            "heat_number": inv.heat_number,
                        })

            if matching_inv:
                total_avail = sum(m["quantity"] for m in matching_inv)
                matches.append({
                    "material": f"{shape} {dims}",
                    "grade": grade,
                    "needed_qty": need["needed_qty"],
                    "available_qty": total_avail,
                    "can_cover": total_avail >= need["needed_qty"],
                    "inventory_items": matching_inv,
                })

        return {
            "nest_run_id": latest_run.id,
            "matches": matches,
            "total_buy_groups": len(buy_groups),
            "groups_with_stock": len(matches),
        }
    finally:
        db.close()


@router.post("/projects/{project_id}/use-inventory")
def use_inventory_for_project(project_id: int, data: dict = Body(...)):
    """
    PM selects which inventory items to use instead of buying.
    data: { "items": [{"inv_id": 1, "qty": 5}, ...] }
    After this, PM can create RFQ for remaining items only.
    """
    db = get_db()
    try:
        items = data.get("items", [])
        used = 0
        for item in items:
            inv = db.query(MaterialInventory).get(item["inv_id"])
            if not inv or inv.status != "available":
                continue

            use_qty = item.get("qty", inv.quantity or 1)
            total = inv.quantity or 1

            if use_qty >= total:
                inv.status = "reserved"
                inv.reserved_project_id = project_id
                inv.reserved_date = datetime.utcnow()
                inv.reserved_by = data.get("reserved_by", "")
                used += total
            else:
                # Split
                inv.quantity = total - use_qty
                new_inv = MaterialInventory(
                    barcode=_generate_inv_barcode(),
                    source_type=inv.source_type,
                    member_size=inv.member_size,
                    shape=inv.shape,
                    dimensions=inv.dimensions,
                    length_display=inv.length_display,
                    length_inches=inv.length_inches,
                    width_inches=inv.width_inches,
                    quantity=use_qty,
                    weight=inv.weight,
                    grade=inv.grade,
                    heat_number=inv.heat_number,
                    location=inv.location,
                    status="reserved",
                    reserved_project_id=project_id,
                    reserved_date=datetime.utcnow(),
                    reserved_by=data.get("reserved_by", ""),
                    added_date=inv.added_date,
                    added_by=inv.added_by,
                    notes=inv.notes,
                )
                db.add(new_inv)
                used += use_qty

        db.commit()
        return {"success": True, "items_reserved": used}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  PRODUCTION FOLDERS — Shop Floor Integration
# ═══════════════════════════════════════════════════════════════

# Station hierarchy for frontend
PRODUCTION_STATIONS = {
    "Yard": {"color": "#78716c", "group": "Yard"},
    "Yard - Bay 1": {"color": "#78716c", "group": "Yard"},
    "Yard - Bay 2": {"color": "#78716c", "group": "Yard"},
    "Yard - Bay 3": {"color": "#78716c", "group": "Yard"},
    "Yard - Rack": {"color": "#78716c", "group": "Yard"},
    "Plasma Plate": {"color": "#0e7490", "group": "PreProduction"},
    "Plasma Beam Line": {"color": "#0369a1", "group": "PreProduction"},
    "Piranha Laser": {"color": "#7c3aed", "group": "PreProduction"},
    "Press Brake": {"color": "#6d28d9", "group": "PreProduction"},
    "Fit": {"color": "#c026d3", "group": "Assembly/QC"},
    "QC": {"color": "#ea580c", "group": "Assembly/QC"},
    "Weld": {"color": "#dc2626", "group": "Assembly/QC"},
    "CWI - Visual": {"color": "#f97316", "group": "Assembly/QC"},
    "CWI - NDT": {"color": "#ef4444", "group": "Assembly/QC"},
    "Prime": {"color": "#0891b2", "group": "Finish"},
    "Galvanize": {"color": "#ca8a04", "group": "Finish"},
    "Powder Coat": {"color": "#7c3aed", "group": "Finish"},
    "Off Site Painting": {"color": "#a16207", "group": "Finish"},
    "Ready to Ship": {"color": "#16a34a", "group": "Shipping"},
    "Shipped - To Coater": {"color": "#059669", "group": "Shipping"},
    "Shipped - To Customer": {"color": "#22c55e", "group": "Shipping"},
    "Delivered": {"color": "#15803d", "group": "Shipping"},
}

@router.get("/stations")
def get_stations():
    """Return station hierarchy for frontend."""
    return PRODUCTION_STATIONS


class FolderCreate(BaseModel):
    folder_number: int
    folder_name: str = ""
    shop: str = "Shop 1"
    assigned_to: str = ""
    notes: str = ""
    piece_marks: List[str] = []


@router.get("/projects/{project_id}/folders")
def list_folders(project_id: int):
    """List all production folders for a project."""
    db = get_db()
    try:
        folders = db.query(ProductionFolder).filter(
            ProductionFolder.project_id == project_id
        ).order_by(ProductionFolder.shop, ProductionFolder.priority, ProductionFolder.folder_number).all()

        result = []
        for f in folders:
            items = db.query(ProductionFolderItem).filter(ProductionFolderItem.folder_id == f.id).all()
            # Enrich items with assembly data
            enriched = []
            for item in items:
                asm = None
                if item.assembly_id:
                    asm = db.query(Assembly).get(item.assembly_id)
                enriched.append({
                    "id": item.id,
                    "piece_mark": item.piece_mark,
                    "status": item.status,
                    "station": item.station or f.station,
                    "completed_date": item.completed_date.isoformat() if item.completed_date else None,
                    "assembly_id": item.assembly_id,
                    "assembly_name": asm.assembly_name if asm else None,
                    "assembly_qty": asm.assembly_quantity if asm else None,
                    "current_station": asm.current_station if asm else None,
                    "notes": item.notes,
                })

            completed_count = sum(1 for i in items if i.status == "completed")
            result.append({
                "id": f.id,
                "folder_number": f.folder_number,
                "folder_name": f.folder_name or f"Folder {f.folder_number}",
                "shop": f.shop,
                "station": f.station,
                "sub_location": f.sub_location,
                "status": f.status,
                "priority": f.priority,
                "assigned_to": f.assigned_to,
                "completed_date": f.completed_date.isoformat() if f.completed_date else None,
                "completed_by": f.completed_by,
                "notes": f.notes,
                "items": enriched,
                "total_items": len(items),
                "completed_items": completed_count,
            })
        return result
    finally:
        db.close()


@router.post("/projects/{project_id}/folders")
def create_folder(project_id: int, data: FolderCreate):
    """Create a production folder and assign piece marks."""
    db = get_db()
    try:
        folder = ProductionFolder(
            project_id=project_id,
            folder_number=data.folder_number,
            folder_name=data.folder_name or f"Folder {data.folder_number}",
            shop=data.shop,
            assigned_to=data.assigned_to,
            notes=data.notes,
        )
        db.add(folder)
        db.flush()

        for mark in data.piece_marks:
            mark = mark.strip()
            if not mark:
                continue
            # Try to find the assembly by mark
            asm = db.query(Assembly).filter(
                Assembly.project_id == project_id,
                Assembly.assembly_mark.ilike(f"%{mark}%")
            ).first()

            item = ProductionFolderItem(
                folder_id=folder.id,
                assembly_id=asm.id if asm else None,
                piece_mark=mark,
                status="pending",
            )
            db.add(item)

        db.commit()
        db.refresh(folder)
        return {"success": True, "folder_id": folder.id, "items_added": len(data.piece_marks)}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.put("/folders/{folder_id}")
def update_folder(folder_id: int, data: FolderCreate):
    """Update folder details."""
    db = get_db()
    try:
        folder = db.query(ProductionFolder).get(folder_id)
        if not folder:
            raise HTTPException(404, "Folder not found")
        folder.folder_name = data.folder_name or f"Folder {data.folder_number}"
        folder.shop = data.shop
        folder.assigned_to = data.assigned_to
        folder.notes = data.notes
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/folders/{folder_id}/add-marks")
def add_marks_to_folder(folder_id: int, data: dict = Body(...)):
    """Add piece marks to an existing folder. data: {marks: ['A1','A2']}"""
    db = get_db()
    try:
        folder = db.query(ProductionFolder).get(folder_id)
        if not folder:
            raise HTTPException(404, "Folder not found")

        marks = data.get("marks", [])
        added = 0
        for mark in marks:
            mark = mark.strip()
            if not mark:
                continue
            # Check if mark already in this folder
            existing = db.query(ProductionFolderItem).filter(
                ProductionFolderItem.folder_id == folder_id,
                ProductionFolderItem.piece_mark == mark,
            ).first()
            if existing:
                continue

            asm = db.query(Assembly).filter(
                Assembly.project_id == folder.project_id,
                Assembly.assembly_mark.ilike(f"%{mark}%")
            ).first()

            item = ProductionFolderItem(
                folder_id=folder_id,
                assembly_id=asm.id if asm else None,
                piece_mark=mark,
                status="pending",
            )
            db.add(item)
            added += 1

        db.commit()
        return {"success": True, "added": added}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.delete("/folders/{folder_id}/items/{item_id}")
def remove_folder_item(folder_id: int, item_id: int):
    db = get_db()
    try:
        item = db.query(ProductionFolderItem).get(item_id)
        if not item or item.folder_id != folder_id:
            raise HTTPException(404, "Item not found")
        db.delete(item)
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/folders/{folder_id}/move-station")
def move_folder_station(
    folder_id: int,
    station: str = Form(...),
    sub_location: str = Form(""),
    moved_by: str = Form(""),
):
    """Move entire folder to a new station. Updates all items + linked assemblies."""
    db = get_db()
    try:
        folder = db.query(ProductionFolder).get(folder_id)
        if not folder:
            raise HTTPException(404, "Folder not found")

        folder.station = station
        folder.sub_location = sub_location
        if folder.status == "open":
            folder.status = "in_progress"

        # Update all items in the folder
        items = db.query(ProductionFolderItem).filter(
            ProductionFolderItem.folder_id == folder_id,
            ProductionFolderItem.status != "completed",
        ).all()

        for item in items:
            item.station = station
            # Update linked assembly in the project tracker
            if item.assembly_id:
                asm = db.query(Assembly).get(item.assembly_id)
                if asm:
                    asm.current_station = station
                    # Record scan event for audit trail
                    scan = ScanEvent(
                        assembly_id=asm.id,
                        station=station,
                        scanned_by=moved_by or "Folder Move",
                        notes=f"Folder {folder.folder_number} moved to {station}" + (f" ({sub_location})" if sub_location else ""),
                    )
                    db.add(scan)

        db.commit()
        return {"success": True, "items_updated": len(items)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/folders/{folder_id}/complete")
def complete_folder(
    folder_id: int,
    completed_by: str = Form(""),
    next_station: str = Form(""),
):
    """Mark folder as completed. All items get completed + assemblies updated."""
    db = get_db()
    try:
        folder = db.query(ProductionFolder).get(folder_id)
        if not folder:
            raise HTTPException(404, "Folder not found")

        now = datetime.utcnow()
        folder.status = "completed"
        folder.completed_date = now
        folder.completed_by = completed_by

        items = db.query(ProductionFolderItem).filter(
            ProductionFolderItem.folder_id == folder_id
        ).all()

        station = next_station or folder.station
        for item in items:
            item.status = "completed"
            item.completed_date = now
            item.station = station
            # Update assembly in project tracker
            if item.assembly_id:
                asm = db.query(Assembly).get(item.assembly_id)
                if asm:
                    asm.current_station = station
                    scan = ScanEvent(
                        assembly_id=asm.id,
                        station=station,
                        scanned_by=completed_by or "Folder Complete",
                        notes=f"Folder {folder.folder_number} completed",
                    )
                    db.add(scan)

        db.commit()
        return {"success": True, "items_completed": len(items)}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/folders/{folder_id}/complete-item/{item_id}")
def complete_folder_item(
    folder_id: int,
    item_id: int,
    completed_by: str = Form(""),
    station: str = Form(""),
):
    """Mark a single item in a folder as completed."""
    db = get_db()
    try:
        item = db.query(ProductionFolderItem).get(item_id)
        if not item or item.folder_id != folder_id:
            raise HTTPException(404, "Item not found")

        item.status = "completed"
        item.completed_date = datetime.utcnow()
        if station:
            item.station = station

        # Update assembly
        if item.assembly_id:
            asm = db.query(Assembly).get(item.assembly_id)
            if asm and station:
                asm.current_station = station
                scan = ScanEvent(
                    assembly_id=asm.id,
                    station=station,
                    scanned_by=completed_by or "Item Complete",
                    notes=f"Completed in Folder {db.query(ProductionFolder).get(folder_id).folder_number}",
                )
                db.add(scan)

        # Check if all items in folder are now completed
        folder = db.query(ProductionFolder).get(folder_id)
        remaining = db.query(ProductionFolderItem).filter(
            ProductionFolderItem.folder_id == folder_id,
            ProductionFolderItem.status != "completed",
        ).count()
        if remaining == 0:
            folder.status = "completed"
            folder.completed_date = datetime.utcnow()
            folder.completed_by = completed_by

        db.commit()
        return {"success": True, "folder_complete": remaining == 0}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: int):
    db = get_db()
    try:
        folder = db.query(ProductionFolder).get(folder_id)
        if not folder:
            raise HTTPException(404, "Folder not found")
        db.delete(folder)
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/folders/{folder_id}/reorder")
def reorder_folder(folder_id: int, priority: int = Form(...)):
    db = get_db()
    try:
        folder = db.query(ProductionFolder).get(folder_id)
        if not folder:
            raise HTTPException(404, "Folder not found")
        folder.priority = priority
        db.commit()
        return {"success": True}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
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


# ═══════════════════════════════════════════════════════════════
#  AIA G702/G703 INVOICING
# ═══════════════════════════════════════════════════════════════

# ─── SCHEDULE OF VALUES (SOV) ───────────────────────────

class SOVCreate(BaseModel):
    item_number: str
    description: str
    scheduled_value: float = 0

class SOVBulkCreate(BaseModel):
    lines: List[SOVCreate]


@router.get("/projects/{project_id}/sov")
def get_sov(project_id: int):
    """Get Schedule of Values for a project."""
    db = get_db()
    try:
        lines = db.query(SOVLine).filter(
            SOVLine.project_id == project_id
        ).order_by(SOVLine.sort_order, SOVLine.id).all()
        return [{
            "id": l.id,
            "item_number": l.item_number,
            "description": l.description,
            "scheduled_value": l.scheduled_value,
            "sort_order": l.sort_order,
        } for l in lines]
    finally:
        db.close()


@router.post("/projects/{project_id}/sov")
def create_sov_line(project_id: int, data: SOVCreate):
    """Add a single SOV line item."""
    db = get_db()
    try:
        max_order = db.query(func.max(SOVLine.sort_order)).filter(
            SOVLine.project_id == project_id).scalar() or 0
        line = SOVLine(
            project_id=project_id,
            item_number=data.item_number,
            description=data.description,
            scheduled_value=data.scheduled_value,
            sort_order=max_order + 1,
        )
        db.add(line)
        db.commit()
        db.refresh(line)
        return {"success": True, "id": line.id}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.post("/projects/{project_id}/sov/bulk")
def create_sov_bulk(project_id: int, data: SOVBulkCreate):
    """Bulk create SOV lines."""
    db = get_db()
    try:
        max_order = db.query(func.max(SOVLine.sort_order)).filter(
            SOVLine.project_id == project_id).scalar() or 0
        added = 0
        for i, line in enumerate(data.lines):
            sov = SOVLine(
                project_id=project_id,
                item_number=line.item_number,
                description=line.description,
                scheduled_value=line.scheduled_value,
                sort_order=max_order + i + 1,
            )
            db.add(sov)
            added += 1
        db.commit()
        return {"success": True, "added": added}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.put("/sov/{sov_id}")
def update_sov_line(sov_id: int, data: SOVCreate):
    """Update a SOV line item."""
    db = get_db()
    try:
        line = db.query(SOVLine).get(sov_id)
        if not line:
            raise HTTPException(404, "SOV line not found")
        line.item_number = data.item_number
        line.description = data.description
        line.scheduled_value = data.scheduled_value
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.delete("/sov/{sov_id}")
def delete_sov_line(sov_id: int):
    db = get_db()
    try:
        line = db.query(SOVLine).get(sov_id)
        if not line:
            raise HTTPException(404)
        db.delete(line)
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


# ─── INVOICES (G702/G703) ───────────────────────────────

class InvoiceCreate(BaseModel):
    period_from: str = ""
    period_to: str = ""
    retainage_pct: float = 10.0
    notes: str = ""


@router.get("/projects/{project_id}/invoices")
def list_invoices(project_id: int):
    """List all invoices for a project."""
    db = get_db()
    try:
        invoices = db.query(Invoice).filter(
            Invoice.project_id == project_id
        ).order_by(desc(Invoice.application_number)).all()
        return [{
            "id": inv.id,
            "application_number": inv.application_number,
            "period_from": inv.period_from.isoformat() if inv.period_from else None,
            "period_to": inv.period_to.isoformat() if inv.period_to else None,
            "original_contract_sum": inv.original_contract_sum,
            "net_change_orders": inv.net_change_orders,
            "contract_sum_to_date": inv.contract_sum_to_date,
            "retainage_pct": inv.retainage_pct,
            "total_retainage": inv.total_retainage,
            "total_completed_and_stored": inv.total_completed_and_stored,
            "less_previous_certificates": inv.less_previous_certificates,
            "current_payment_due": inv.current_payment_due,
            "balance_to_finish": inv.balance_to_finish,
            "status": inv.status,
            "submitted_date": inv.submitted_date.isoformat() if inv.submitted_date else None,
            "notes": inv.notes,
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
        } for inv in invoices]
    finally:
        db.close()


@router.post("/projects/{project_id}/invoices")
def create_invoice(project_id: int, data: InvoiceCreate):
    """Create a new pay application from current SOV."""
    db = get_db()
    try:
        # Get SOV lines
        sov_lines = db.query(SOVLine).filter(
            SOVLine.project_id == project_id
        ).order_by(SOVLine.sort_order, SOVLine.id).all()
        if not sov_lines:
            raise HTTPException(400, "No Schedule of Values found. Create SOV first.")

        # Determine app number
        max_app = db.query(func.max(Invoice.application_number)).filter(
            Invoice.project_id == project_id).scalar() or 0
        app_num = max_app + 1

        # Calculate previous applications total from prior invoices
        prev_invoices = db.query(Invoice).filter(
            Invoice.project_id == project_id,
            Invoice.application_number < app_num,
        ).all()

        # Build lookup: sov_line_id -> total completed in previous apps
        prev_by_sov = {}
        for prev_inv in prev_invoices:
            for li in prev_inv.line_items:
                prev_by_sov[li.sov_line_id] = prev_by_sov.get(li.sov_line_id, 0) + li.this_period + li.materials_stored

        # Original contract sum = sum of all SOV scheduled values
        original_sum = sum(l.scheduled_value for l in sov_lines)
        prev_certs = sum(inv.current_payment_due for inv in prev_invoices)

        # Calculate net change orders from approved COs
        net_cos = db.query(func.sum(ChangeOrder.cost_impact)).filter(
            ChangeOrder.project_id == project_id,
            ChangeOrder.status == "Approved"
        ).scalar() or 0

        # Parse dates
        pf = None
        pt = None
        try:
            if data.period_from: pf = date.fromisoformat(data.period_from)
            if data.period_to: pt = date.fromisoformat(data.period_to)
        except: pass

        invoice = Invoice(
            project_id=project_id,
            application_number=app_num,
            period_from=pf,
            period_to=pt,
            original_contract_sum=original_sum,
            net_change_orders=net_cos,
            contract_sum_to_date=original_sum + net_cos,
            retainage_pct=data.retainage_pct,
            less_previous_certificates=prev_certs,
            notes=data.notes,
        )
        db.add(invoice)
        db.flush()

        # Create line items for each SOV line
        for sov in sov_lines:
            prev_amount = prev_by_sov.get(sov.id, 0)
            li = InvoiceLineItem(
                invoice_id=invoice.id,
                sov_line_id=sov.id,
                item_number=sov.item_number,
                description=sov.description,
                scheduled_value=sov.scheduled_value,
                previous_applications=prev_amount,
                this_period=0,
                materials_stored=0,
                total_completed=prev_amount,
                percent_complete=round(prev_amount / sov.scheduled_value * 100, 1) if sov.scheduled_value else 0,
                balance_to_finish=sov.scheduled_value - prev_amount,
                retainage=round(prev_amount * data.retainage_pct / 100, 2),
            )
            db.add(li)

        db.commit()
        db.refresh(invoice)
        return {"success": True, "invoice_id": invoice.id, "application_number": app_num}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int):
    """Get full invoice with G703 line items."""
    db = get_db()
    try:
        inv = db.query(Invoice).get(invoice_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")

        lines = db.query(InvoiceLineItem).filter(
            InvoiceLineItem.invoice_id == invoice_id
        ).order_by(InvoiceLineItem.id).all()

        return {
            "id": inv.id,
            "application_number": inv.application_number,
            "period_from": inv.period_from.isoformat() if inv.period_from else None,
            "period_to": inv.period_to.isoformat() if inv.period_to else None,
            "original_contract_sum": inv.original_contract_sum,
            "net_change_orders": inv.net_change_orders,
            "contract_sum_to_date": inv.contract_sum_to_date,
            "retainage_pct": inv.retainage_pct,
            "retainage_on_completed": inv.retainage_on_completed,
            "retainage_on_stored": inv.retainage_on_stored,
            "total_retainage": inv.total_retainage,
            "total_completed_and_stored": inv.total_completed_and_stored,
            "less_previous_certificates": inv.less_previous_certificates,
            "current_payment_due": inv.current_payment_due,
            "balance_to_finish": inv.balance_to_finish,
            "status": inv.status,
            "notes": inv.notes,
            "line_items": [{
                "id": li.id,
                "sov_line_id": li.sov_line_id,
                "item_number": li.item_number,
                "description": li.description,
                "scheduled_value": li.scheduled_value,
                "previous_applications": li.previous_applications,
                "this_period": li.this_period,
                "materials_stored": li.materials_stored,
                "total_completed": li.total_completed,
                "percent_complete": li.percent_complete,
                "balance_to_finish": li.balance_to_finish,
                "retainage": li.retainage,
            } for li in lines],
        }
    finally:
        db.close()


class LineItemUpdate(BaseModel):
    this_period: float = 0
    materials_stored: float = 0


@router.put("/invoices/{invoice_id}/lines")
def update_invoice_lines(invoice_id: int, updates: dict = Body(...)):
    """Update G703 line items. Body: {line_item_id: {this_period, materials_stored}, ...}"""
    db = get_db()
    try:
        inv = db.query(Invoice).get(invoice_id)
        if not inv:
            raise HTTPException(404, "Invoice not found")

        total_completed = 0
        total_retainage_completed = 0
        total_retainage_stored = 0

        lines = db.query(InvoiceLineItem).filter(
            InvoiceLineItem.invoice_id == invoice_id
        ).all()

        for li in lines:
            key = str(li.id)
            if key in updates:
                u = updates[key]
                li.this_period = u.get("this_period", li.this_period)
                li.materials_stored = u.get("materials_stored", li.materials_stored)

            # Recalculate
            li.total_completed = li.previous_applications + li.this_period + li.materials_stored
            li.percent_complete = round(li.total_completed / li.scheduled_value * 100, 1) if li.scheduled_value else 0
            li.balance_to_finish = li.scheduled_value - li.total_completed
            li.retainage = round(li.total_completed * inv.retainage_pct / 100, 2)

            total_completed += li.total_completed
            work_done = li.previous_applications + li.this_period
            total_retainage_completed += round(work_done * inv.retainage_pct / 100, 2)
            total_retainage_stored += round(li.materials_stored * inv.retainage_pct / 100, 2)

        # Update G702 summary
        inv.total_completed_and_stored = total_completed
        inv.retainage_on_completed = total_retainage_completed
        inv.retainage_on_stored = total_retainage_stored
        inv.total_retainage = total_retainage_completed + total_retainage_stored
        inv.balance_to_finish = inv.contract_sum_to_date - total_completed
        inv.current_payment_due = total_completed - inv.total_retainage - inv.less_previous_certificates

        db.commit()
        return {"success": True, "current_payment_due": inv.current_payment_due}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.put("/invoices/{invoice_id}/change-orders")
def update_change_orders(invoice_id: int, net_change_orders: float = Form(...)):
    """Update net change order amount on G702."""
    db = get_db()
    try:
        inv = db.query(Invoice).get(invoice_id)
        if not inv:
            raise HTTPException(404)
        inv.net_change_orders = net_change_orders
        inv.contract_sum_to_date = inv.original_contract_sum + net_change_orders
        inv.balance_to_finish = inv.contract_sum_to_date - inv.total_completed_and_stored
        inv.current_payment_due = inv.total_completed_and_stored - inv.total_retainage - inv.less_previous_certificates
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.put("/invoices/{invoice_id}/submit")
def submit_invoice(invoice_id: int):
    """Mark invoice as submitted."""
    db = get_db()
    try:
        inv = db.query(Invoice).get(invoice_id)
        if not inv:
            raise HTTPException(404)
        inv.status = "submitted"
        inv.submitted_date = date.today()
        db.commit()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.put("/invoices/{invoice_id}/approve")
def approve_invoice(invoice_id: int):
    db = get_db()
    try:
        inv = db.query(Invoice).get(invoice_id)
        if not inv: raise HTTPException(404)
        inv.status = "approved"
        db.commit()
        return {"success": True}
    except HTTPException: raise
    finally: db.close()


@router.put("/invoices/{invoice_id}/paid")
def mark_invoice_paid(invoice_id: int):
    db = get_db()
    try:
        inv = db.query(Invoice).get(invoice_id)
        if not inv: raise HTTPException(404)
        inv.status = "paid"
        db.commit()
        return {"success": True}
    except HTTPException: raise
    finally: db.close()


@router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int):
    db = get_db()
    try:
        inv = db.query(Invoice).get(invoice_id)
        if not inv: raise HTTPException(404)
        if inv.status != "draft":
            raise HTTPException(400, "Can only delete draft invoices")
        db.delete(inv)
        db.commit()
        return {"success": True}
    except HTTPException: raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally: db.close()


# ─── CHANGE ORDERS ──────────────────────────────────────

class COCreate(BaseModel):
    description: str
    amount: float = 0
    status: str = "Draft"
    date_submitted: str = ""
    notes: str = ""


@router.get("/projects/{project_id}/change-orders")
def list_change_orders(project_id: int):
    db = get_db()
    try:
        cos = db.query(ChangeOrder).filter(
            ChangeOrder.project_id == project_id
        ).order_by(ChangeOrder.co_number).all()
        return [{
            "id": co.id,
            "co_number": co.co_number,
            "description": co.title or co.description or "",
            "amount": co.cost_impact or 0,
            "status": co.status,
            "date_submitted": co.submitted_date.isoformat() if co.submitted_date else None,
            "date_approved": co.approved_date.isoformat() if co.approved_date else None,
            "notes": co.description or "",
        } for co in cos]
    finally:
        db.close()


@router.post("/projects/{project_id}/change-orders")
def create_change_order(project_id: int, data: COCreate):
    db = get_db()
    try:
        max_num = db.query(func.max(ChangeOrder.id)).filter(
            ChangeOrder.project_id == project_id).scalar() or 0
        ds = None
        try:
            if data.date_submitted: ds = date.fromisoformat(data.date_submitted)
        except: pass
        co = ChangeOrder(
            project_id=project_id,
            co_number=str(max_num + 1),
            title=data.description,
            cost_impact=data.amount,
            status=data.status,
            submitted_date=ds,
            description=data.notes,
        )
        db.add(co)
        db.commit()
        db.refresh(co)
        return {"success": True, "id": co.id, "co_number": co.co_number}
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.put("/change-orders/{co_id}")
def update_change_order(co_id: int, data: COCreate):
    db = get_db()
    try:
        co = db.query(ChangeOrder).get(co_id)
        if not co: raise HTTPException(404)
        co.title = data.description
        co.cost_impact = data.amount
        co.status = data.status
        co.description = data.notes
        try:
            if data.date_submitted: co.submitted_date = date.fromisoformat(data.date_submitted)
        except: pass
        if data.status == "Approved" and not co.approved_date:
            co.approved_date = date.today()
        db.commit()
        return {"success": True}
    except HTTPException: raise
    except Exception as e:
        db.rollback()
        raise HTTPException(500, str(e))
    finally:
        db.close()


@router.delete("/change-orders/{co_id}")
def delete_change_order(co_id: int):
    db = get_db()
    try:
        co = db.query(ChangeOrder).get(co_id)
        if not co: raise HTTPException(404)
        db.delete(co)
        db.commit()
        return {"success": True}
    except HTTPException: raise
    finally: db.close()


@router.get("/projects/{project_id}/approved-co-total")
def get_approved_co_total(project_id: int):
    """Get net total of approved change orders."""
    db = get_db()
    try:
        total = db.query(func.sum(ChangeOrder.cost_impact)).filter(
            ChangeOrder.project_id == project_id,
            ChangeOrder.status == "Approved"
        ).scalar() or 0
        return {"total": total}
    finally:
        db.close()


# ─── AIA G702/G703 PDF EXPORT ───────────────────────────

@router.get("/invoices/{invoice_id}/pdf")
def export_invoice_pdf(invoice_id: int):
    """Generate official AIA G702/G703 PDF."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    from reportlab.lib import colors
    import io
    from starlette.responses import StreamingResponse

    db = get_db()
    try:
        inv = db.query(Invoice).get(invoice_id)
        if not inv: raise HTTPException(404)

        # Get project info
        from models import Project
        project = db.query(Project).get(inv.project_id)

        lines = db.query(InvoiceLineItem).filter(
            InvoiceLineItem.invoice_id == invoice_id
        ).order_by(InvoiceLineItem.id).all()

        # Get change orders for this project
        cos = db.query(ChangeOrder).filter(
            ChangeOrder.project_id == inv.project_id,
            ChangeOrder.status == "Approved"
        ).order_by(ChangeOrder.co_number).all()

        buf = io.BytesIO()
        w, h = letter
        c = canvas.Canvas(buf, pagesize=letter)

        def fmt(n):
            if n is None: n = 0
            neg = n < 0
            s = f"${abs(n):,.2f}"
            return f"({s})" if neg else s

        def draw_box(x, y, width, height, label="", value="", bold_value=False):
            c.setStrokeColor(colors.black)
            c.setLineWidth(0.5)
            c.rect(x, y, width, height)
            if label:
                c.setFont("Helvetica", 6)
                c.drawString(x + 2, y + height - 8, label)
            if value:
                c.setFont("Helvetica-Bold" if bold_value else "Helvetica", 9)
                c.drawString(x + 4, y + 3, str(value))

        # ═══ PAGE 1: G702 ═══
        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(w/2, h - 36, "AIA DOCUMENT G702")
        c.setFont("Helvetica", 8)
        c.drawCentredString(w/2, h - 48, "APPLICATION AND CERTIFICATE FOR PAYMENT")

        # Header boxes
        top = h - 65
        left = 36
        col2 = w/2 + 18
        bw = w/2 - 54  # box width
        bh = 36

        draw_box(left, top - bh, bw, bh, "TO OWNER:", project.customer if project else "")
        draw_box(col2, top - bh, bw, bh, "APPLICATION NO:", str(inv.application_number))

        draw_box(left, top - bh*2, bw, bh, "FROM CONTRACTOR:", "SSE - Steel Structural Engineering")
        draw_box(col2, top - bh*2, bw, bh, "PERIOD FROM:", inv.period_from.isoformat() if inv.period_from else "")

        draw_box(left, top - bh*3, bw, bh, "PROJECT:", f"{project.job_number} - {project.name}" if project else "")
        draw_box(col2, top - bh*3, bw, bh, "PERIOD TO:", inv.period_to.isoformat() if inv.period_to else "")

        draw_box(left, top - bh*4, bw, bh, "CONTRACT FOR:", "Structural Steel Fabrication")
        draw_box(col2, top - bh*4, bw, bh, "CONTRACT DATE:", "")

        # Contractor's Application section
        sy = top - bh*4 - 30
        c.setFont("Helvetica-Bold", 9)
        c.drawString(left, sy, "CONTRACTOR'S APPLICATION FOR PAYMENT")

        # G702 Line items
        ly = sy - 20
        lh = 22
        numW = 24
        descW = bw + 60
        valW = w - 36 - left - numW - descW - 10

        g702_lines = [
            ("1.", "ORIGINAL CONTRACT SUM", fmt(inv.original_contract_sum)),
            ("2.", "Net change by Change Orders", fmt(inv.net_change_orders)),
            ("3.", "CONTRACT SUM TO DATE (Line 1 + 2)", fmt(inv.contract_sum_to_date)),
            ("4.", "TOTAL COMPLETED & STORED TO DATE", fmt(inv.total_completed_and_stored)),
            ("", "(Column G on G703)", ""),
            ("5a.", f"Retainage: {inv.retainage_pct}% of Completed Work", fmt(inv.retainage_on_completed)),
            ("5b.", f"Retainage: {inv.retainage_pct}% of Stored Material", fmt(inv.retainage_on_stored)),
            ("", "TOTAL RETAINAGE (Lines 5a + 5b)", fmt(inv.total_retainage)),
            ("6.", "TOTAL EARNED LESS RETAINAGE", fmt(inv.total_completed_and_stored - inv.total_retainage)),
            ("7.", "LESS PREVIOUS CERTIFICATES FOR PAYMENT", fmt(inv.less_previous_certificates)),
            ("8.", "CURRENT PAYMENT DUE", fmt(inv.current_payment_due)),
            ("9.", "BALANCE TO FINISH, INCLUDING RETAINAGE", fmt(inv.balance_to_finish + inv.total_retainage)),
        ]

        for i, (num, desc, val) in enumerate(g702_lines):
            y = ly - (i * lh)
            is_total = num in ("8.", "") and "TOTAL" in desc
            is_payment = num == "8."
            c.setStrokeColor(colors.black)
            c.setLineWidth(0.5)
            c.rect(left, y - lh + 4, w - 72, lh)

            c.setFont("Helvetica-Bold" if is_payment else "Helvetica", 8)
            c.drawString(left + 3, y - 8, num)
            c.drawString(left + numW, y - 8, desc)

            if val:
                c.setFont("Helvetica-Bold" if is_payment else "Helvetica", 9)
                c.drawRightString(w - 42, y - 8, val)

        # Change orders summary if any
        if cos:
            coy = ly - (len(g702_lines) * lh) - 20
            c.setFont("Helvetica-Bold", 8)
            c.drawString(left, coy, "CHANGE ORDER SUMMARY:")
            c.setFont("Helvetica", 7)
            for i, co in enumerate(cos):
                c.drawString(left + 10, coy - 12 - (i * 11),
                    f"CO #{co.co_number}: {co.title} — {fmt(co.cost_impact)} ({co.status})")

        # Signature lines
        sig_y = 80
        c.setLineWidth(0.5)
        c.line(left, sig_y, left + 200, sig_y)
        c.line(w/2 + 20, sig_y, w - 36, sig_y)
        c.setFont("Helvetica", 7)
        c.drawString(left, sig_y - 10, "Contractor Signature & Date")
        c.drawString(w/2 + 20, sig_y - 10, "Owner Signature & Date")

        c.showPage()

        # ═══ PAGE 2+: G703 Continuation Sheet ═══
        def draw_g703_header(page_num=1):
            c.setFont("Helvetica-Bold", 12)
            c.drawCentredString(w/2, h - 30, "AIA DOCUMENT G703 — CONTINUATION SHEET")
            c.setFont("Helvetica", 7)
            c.drawCentredString(w/2, h - 42, f"Application No: {inv.application_number} | "
                f"Application Date: {inv.period_to.isoformat() if inv.period_to else ''} | "
                f"Project: {project.job_number if project else ''} - {project.name if project else ''}")
            if page_num > 1:
                c.drawRightString(w - 36, h - 42, f"Page {page_num}")

        # Column definitions for G703
        cols = [
            ("A", "Item\nNo.", 30),
            ("B", "Description of Work", 140),
            ("C", "Scheduled\nValue", 72),
            ("D", "Work Completed\nPrevious Apps", 72),
            ("E", "Work Completed\nThis Period", 72),
            ("F", "Materials\nStored", 62),
            ("G", "Total\nCompleted", 72),
            ("G/C", "%", 28),
            ("H", "Balance To\nFinish", 72),
        ]

        def draw_g703_table_header(y):
            x = left
            c.setFillColor(colors.Color(0.9, 0.9, 0.9))
            c.rect(left, y - 28, w - 72, 28, fill=1)
            c.setFillColor(colors.black)
            c.setFont("Helvetica-Bold", 6)
            for col_letter, col_name, col_w in cols:
                c.drawCentredString(x + col_w/2, y - 10, col_letter)
                c.setFont("Helvetica", 5)
                for j, line in enumerate(col_name.split("\n")):
                    c.drawCentredString(x + col_w/2, y - 18 - (j * 7), line)
                c.setFont("Helvetica-Bold", 6)
                c.setStrokeColor(colors.black)
                c.setLineWidth(0.3)
                c.line(x, y, x, y - 28)
                x += col_w
            c.line(x, y, x, y - 28)
            c.rect(left, y - 28, w - 72, 28)
            return y - 28

        draw_g703_header(1)
        table_y = draw_g703_table_header(h - 55)
        row_h = 14
        rows_per_page = int((table_y - 80) / row_h)
        row_count = 0
        page_num = 1

        totals = {"scheduled": 0, "prev": 0, "this": 0, "stored": 0, "total": 0, "balance": 0}

        for li in lines:
            if row_count >= rows_per_page:
                # Draw page totals and start new page
                c.showPage()
                page_num += 1
                draw_g703_header(page_num)
                table_y = draw_g703_table_header(h - 55)
                row_count = 0

            y = table_y - (row_count * row_h)
            x = left

            row_data = [
                (li.item_number, 30, "center"),
                (li.description[:30], 140, "left"),
                (fmt(li.scheduled_value), 72, "right"),
                (fmt(li.previous_applications), 72, "right"),
                (fmt(li.this_period), 72, "right"),
                (fmt(li.materials_stored), 62, "right"),
                (fmt(li.total_completed), 72, "right"),
                (f"{li.percent_complete:.0f}%", 28, "center"),
                (fmt(li.balance_to_finish), 72, "right"),
            ]

            c.setLineWidth(0.3)
            for val, col_w, align in row_data:
                c.line(x, y, x, y - row_h)
                c.setFont("Helvetica", 6)
                if align == "right":
                    c.drawRightString(x + col_w - 3, y - 10, str(val))
                elif align == "center":
                    c.drawCentredString(x + col_w/2, y - 10, str(val))
                else:
                    c.drawString(x + 2, y - 10, str(val))
                x += col_w
            c.line(x, y, x, y - row_h)
            c.line(left, y - row_h, x, y - row_h)

            totals["scheduled"] += li.scheduled_value
            totals["prev"] += li.previous_applications
            totals["this"] += li.this_period
            totals["stored"] += li.materials_stored
            totals["total"] += li.total_completed
            totals["balance"] += li.balance_to_finish
            row_count += 1

        # Totals row
        y = table_y - (row_count * row_h)
        x = left
        c.setFillColor(colors.Color(0.93, 0.93, 0.93))
        c.rect(left, y - row_h - 2, w - 72, row_h + 2, fill=1)
        c.setFillColor(colors.black)

        total_data = [
            ("", 30), ("GRAND TOTAL", 140),
            (fmt(totals["scheduled"]), 72), (fmt(totals["prev"]), 72),
            (fmt(totals["this"]), 72), (fmt(totals["stored"]), 62),
            (fmt(totals["total"]), 72), ("", 28),
            (fmt(totals["balance"]), 72),
        ]
        for val, col_w in total_data:
            c.setFont("Helvetica-Bold", 6)
            if val and val.startswith("$"):
                c.drawRightString(x + col_w - 3, y - 10, val)
            elif val:
                c.drawString(x + 2, y - 10, val)
            x += col_w

        c.save()
        buf.seek(0)

        filename = f"AIA_G702_G703_App{inv.application_number}_{project.job_number if project else 'unknown'}.pdf"
        return StreamingResponse(
            buf,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    finally:
        db.close()
