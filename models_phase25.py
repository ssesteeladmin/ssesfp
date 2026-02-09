"""
SSE Steel Project Tracker - Phase 2.5 Models
Procurement-to-Production Material Lifecycle
"""
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Text, DateTime, Date,
    ForeignKey, JSON, Index, Numeric
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from models import Base
import uuid


# ─── STOCK SIZE LIBRARY ─────────────────────────────────────

class StockConfig(Base):
    """Standard stock sizes available for cutting/nesting."""
    __tablename__ = "tracker_stock_config"
    id = Column(Integer, primary_key=True)
    shape_code = Column(String(20), nullable=False)  # W, HSS, HSSR, RT, PIPE, L, C, MC, S, PL
    nest_type = Column(String(20), default="mult")  # mult, plate
    available_lengths = Column(JSON)  # [20, 24, 40] in feet for linear; [{w:4,l:8},{w:5,l:10}] for plate
    kerf_inches = Column(Float, default=0.125)  # 1/8" default
    notes = Column(Text)
    active = Column(Boolean, default=True)


def seed_stock_config(db_session):
    """Seed default SSE stock configurations if table is empty."""
    if db_session.query(StockConfig).first():
        return  # Already seeded
    
    defaults = [
        # Linear members (Mult)
        {"shape_code": "W", "nest_type": "mult", "available_lengths": [20, 25, 30, 35, 40, 45, 50, 55, 60], "kerf_inches": 0.125, "notes": "W Beams"},
        {"shape_code": "HSS", "nest_type": "mult", "available_lengths": [20, 24, 40, 48], "kerf_inches": 0.125, "notes": "HSS Tube"},
        {"shape_code": "HSSR", "nest_type": "mult", "available_lengths": [20, 24, 40, 48], "kerf_inches": 0.125, "notes": "HSS Round"},
        {"shape_code": "RT", "nest_type": "mult", "available_lengths": [20, 24, 40, 48], "kerf_inches": 0.125, "notes": "Rectangle Tube"},
        {"shape_code": "PIPE", "nest_type": "mult", "available_lengths": [21, 42], "kerf_inches": 0.125, "notes": "Pipe"},
        {"shape_code": "L", "nest_type": "mult", "available_lengths": [20, 40], "kerf_inches": 0.125, "notes": "Angle"},
        {"shape_code": "C", "nest_type": "mult", "available_lengths": [20, 25, 30, 40, 50], "kerf_inches": 0.125, "notes": "Channel"},
        {"shape_code": "MC", "nest_type": "mult", "available_lengths": [20, 25, 30, 40, 50], "kerf_inches": 0.125, "notes": "MC Channel"},
        {"shape_code": "S", "nest_type": "mult", "available_lengths": [20, 25, 30, 35, 40, 45, 50], "kerf_inches": 0.125, "notes": "S Beam"},
        # Plate
        {"shape_code": "PL", "nest_type": "plate", "available_lengths": [
            {"w": 4, "l": 8, "thickness_max": 0.5, "notes": "1/2 and under"},
            {"w": 5, "l": 10, "thickness_max": 0.5, "notes": "1/2 and under"},
            {"w": 4, "l": 8, "thickness_min": 0.75, "thickness_max": 1.0, "notes": "3/4 and 1 inch"},
            {"w": 5, "l": 10, "thickness_min": 0.75, "thickness_max": 1.0, "notes": "3/4 and 1 inch"},
            {"w": 10, "l": 20, "thickness_min": 0.75, "thickness_max": 1.0, "notes": "3/4 and 1 inch"},
            {"w": 4, "l": 8, "thickness_min": 1.25, "thickness_max": 2.0, "notes": "1-1/4 to 2 inch"},
            {"w": 5, "l": 10, "thickness_min": 1.25, "thickness_max": 2.0, "notes": "1-1/4 to 2 inch"},
        ], "kerf_inches": 0.125, "notes": "Plate stock"},
    ]
    
    for cfg in defaults:
        db_session.add(StockConfig(**cfg))
    db_session.commit()


# ─── VENDORS (Enhanced from Company) ──────────────────────────

class Vendor(Base):
    __tablename__ = "tracker_vendors"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    contact_name = Column(String(200))
    address_line1 = Column(String(255))
    address_line2 = Column(String(255))
    city = Column(String(100))
    state = Column(String(50))
    zip_code = Column(String(20))
    phone = Column(String(50))
    fax = Column(String(50))
    email = Column(String(200))
    default_terms = Column(String(50), default="Net 45 days")
    notes = Column(Text)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── NEST RUNS ────────────────────────────────────────────────

class NestRun(Base):
    __tablename__ = "tracker_nest_runs"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("tracker_projects.id"))
    nest_date = Column(DateTime, default=datetime.utcnow)
    operator = Column(String(100))
    machine = Column(String(100))
    status = Column(String(20), default="pending")  # pending, complete
    yield_percentage = Column(Numeric(5, 2))
    total_stock_used = Column(Integer, default=0)
    total_parts_cut = Column(Integer, default=0)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Relationships
    items = relationship("NestRunItem", back_populates="nest_run", cascade="all, delete-orphan")
    drops = relationship("NestRunDrop", back_populates="nest_run", cascade="all, delete-orphan")


class NestRunItem(Base):
    """Individual parts in a nest run"""
    __tablename__ = "tracker_nest_run_items"
    id = Column(Integer, primary_key=True)
    nest_run_id = Column(Integer, ForeignKey("tracker_nest_runs.id"))
    part_id = Column(Integer, ForeignKey("tracker_parts.id"))
    assembly_id = Column(Integer, ForeignKey("tracker_assemblies.id"))
    stock_index = Column(Integer)  # which stock piece this was cut from
    cut_position = Column(Integer)  # order on the stock piece
    cut_length_inches = Column(Float)
    shape = Column(String(100))
    dimensions = Column(String(100))
    grade = Column(String(50))
    part_mark = Column(String(100))
    assembly_mark = Column(String(100))
    quantity = Column(Integer, default=1)
    nest_run = relationship("NestRun", back_populates="items")


class NestRunDrop(Base):
    """Drop/remnant pieces from a nest run"""
    __tablename__ = "tracker_nest_run_drops"
    id = Column(Integer, primary_key=True)
    nest_run_id = Column(Integer, ForeignKey("tracker_nest_runs.id"))
    stock_index = Column(Integer)
    shape = Column(String(100))
    dimensions = Column(String(100))
    grade = Column(String(50))
    stock_length_inches = Column(Float)
    drop_length_inches = Column(Float)
    drop_length_display = Column(String(50))
    weight = Column(Float)
    disposition = Column(String(20))  # NULL, 'inventory', 'scrap'
    disposition_date = Column(DateTime)
    disposition_by = Column(String(100))
    inventory_location = Column(String(100))
    heat_number = Column(String(100))
    nest_run = relationship("NestRun", back_populates="drops")


# ─── ENHANCED RFQ ─────────────────────────────────────────────

class RFQv2(Base):
    __tablename__ = "tracker_rfqs_v2"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("tracker_projects.id"))
    nest_run_id = Column(Integer, ForeignKey("tracker_nest_runs.id"), nullable=True)
    rfq_number = Column(String(50))
    vendor_id = Column(Integer, ForeignKey("tracker_vendors.id"), nullable=True)
    status = Column(String(20), default="draft")  # draft, sent, received, accepted, rejected
    date_sent = Column(DateTime)
    date_due = Column(DateTime)
    date_received = Column(DateTime)
    sub_total = Column(Numeric(12, 2), default=0)
    tax = Column(Numeric(10, 2), default=0)
    freight = Column(Numeric(10, 2), default=0)
    misc_cost = Column(Numeric(10, 2), default=0)
    total_price = Column(Numeric(12, 2), default=0)
    terms_discount = Column(Numeric(10, 2), default=0)
    total_less_discount = Column(Numeric(12, 2), default=0)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Relationships
    items = relationship("RFQItemv2", back_populates="rfq", cascade="all, delete-orphan")
    vendor = relationship("Vendor", foreign_keys=[vendor_id])


class RFQItemv2(Base):
    __tablename__ = "tracker_rfq_items_v2"
    id = Column(Integer, primary_key=True)
    rfq_id = Column(Integer, ForeignKey("tracker_rfqs_v2.id"))
    line_number = Column(Integer)
    qty = Column(Integer, default=1)
    dimensions = Column(String(100))
    grade = Column(String(50))
    length_display = Column(String(50))
    length_inches = Column(Float)
    job_number = Column(String(20))
    weight = Column(Numeric(10, 2))
    unit_price = Column(Numeric(10, 2))
    unit_type = Column(String(10), default="CWT")  # CWT, Ft, Each, Ton
    total_price = Column(Numeric(12, 2))
    excluded = Column(Boolean, default=False)  # excluded from this RFQ
    is_hardware = Column(Boolean, default=False)
    shape = Column(String(100))
    rfq = relationship("RFQv2", back_populates="items")


# ─── RFQ VENDOR QUOTES (for comparison) ─────────────────────

class RFQQuote(Base):
    """Vendor quote uploaded against an RFQ for comparison."""
    __tablename__ = "tracker_rfq_quotes"
    id = Column(Integer, primary_key=True)
    rfq_id = Column(Integer, ForeignKey("tracker_rfqs_v2.id"))
    vendor_id = Column(Integer, ForeignKey("tracker_vendors.id"))
    quote_date = Column(Date)
    expiry_date = Column(Date)
    sub_total = Column(Numeric(12, 2), default=0)
    tax = Column(Numeric(10, 2), default=0)
    freight = Column(Numeric(10, 2), default=0)
    total_price = Column(Numeric(12, 2), default=0)
    lead_time_days = Column(Integer)
    terms = Column(String(100))
    notes = Column(Text)
    quote_pdf = Column(Text)  # base64 encoded PDF
    quote_filename = Column(String(500))
    is_selected = Column(Boolean, default=False)  # winner
    created_at = Column(DateTime, default=datetime.utcnow)
    # Relationships
    vendor = relationship("Vendor", foreign_keys=[vendor_id])
    line_items = relationship("RFQQuoteItem", back_populates="quote", cascade="all, delete-orphan")


class RFQQuoteItem(Base):
    """Line item pricing from a vendor quote."""
    __tablename__ = "tracker_rfq_quote_items"
    id = Column(Integer, primary_key=True)
    quote_id = Column(Integer, ForeignKey("tracker_rfq_quotes.id"))
    rfq_item_id = Column(Integer, ForeignKey("tracker_rfq_items_v2.id"), nullable=True)
    line_number = Column(Integer)
    description = Column(String(255))
    qty = Column(Integer, default=1)
    unit_price = Column(Numeric(10, 2))
    unit_type = Column(String(10), default="CWT")
    total_price = Column(Numeric(12, 2))
    quote = relationship("RFQQuote", back_populates="line_items")


# ─── ENHANCED PURCHASE ORDER ──────────────────────────────────

class POv2(Base):
    __tablename__ = "tracker_pos_v2"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("tracker_projects.id"))
    rfq_id = Column(Integer, ForeignKey("tracker_rfqs_v2.id"), nullable=True)
    po_number = Column(String(50), unique=True)
    vendor_id = Column(Integer, ForeignKey("tracker_vendors.id"), nullable=True)
    ordered_by = Column(String(100))
    order_date = Column(Date)
    fob = Column(String(50), default="Destination")
    ship_via = Column(String(100), default="Truck/Common Carrier")
    terms = Column(String(50), default="Net 45 days")
    order_type = Column(String(50), default="Regular")
    sub_total = Column(Numeric(12, 2), default=0)
    tax = Column(Numeric(10, 2), default=0)
    total_price = Column(Numeric(12, 2), default=0)
    status = Column(String(20), default="draft")  # draft, sent, partial, complete
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Relationships
    items = relationship("POItemv2", back_populates="purchase_order", cascade="all, delete-orphan")
    vendor = relationship("Vendor", foreign_keys=[vendor_id])


class POItemv2(Base):
    __tablename__ = "tracker_po_items_v2"
    id = Column(Integer, primary_key=True)
    po_id = Column(Integer, ForeignKey("tracker_pos_v2.id"))
    line_number = Column(Integer)
    qty = Column(Integer, default=1)
    dimensions = Column(String(100))
    shape = Column(String(100))
    grade = Column(String(50))
    length_display = Column(String(50))
    length_inches = Column(Float)
    job_number = Column(String(20))
    weight = Column(Numeric(10, 2))
    unit_cost = Column(Numeric(10, 2))
    unit_type = Column(String(10), default="CWT")  # CWT, Ft, Each, Ton
    cost = Column(Numeric(12, 2))
    # Receiving
    qty_received = Column(Integer, default=0)
    heat_number = Column(String(100))
    date_received = Column(Date)
    received_by = Column(String(100))
    receiving_barcode = Column(String(100), unique=True)
    receiving_status = Column(String(20), default="pending")  # pending, partial, complete
    purchase_order = relationship("POv2", back_populates="items")


# ─── YARD TAGS ────────────────────────────────────────────────

class YardTag(Base):
    __tablename__ = "tracker_yard_tags"
    id = Column(Integer, primary_key=True)
    po_item_id = Column(Integer, ForeignKey("tracker_po_items_v2.id"))
    tag_barcode = Column(String(100), unique=True)
    member_size = Column(String(100))
    length_display = Column(String(50))
    weight = Column(Numeric(10, 2))
    grade = Column(String(50))
    heat_number = Column(String(100))
    supplier = Column(String(200))
    po_number = Column(String(50))
    job_number = Column(String(20))
    yard_location = Column(String(100))
    status = Column(String(20), default="in_yard")  # in_yard, cutting, cut_complete
    scan_start_cut = Column(DateTime)
    cut_by = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── DROP TAGS ────────────────────────────────────────────────

class DropTag(Base):
    __tablename__ = "tracker_drop_tags"
    id = Column(Integer, primary_key=True)
    yard_tag_id = Column(Integer, ForeignKey("tracker_yard_tags.id"), nullable=True)
    nest_run_drop_id = Column(Integer, ForeignKey("tracker_nest_run_drops.id"), nullable=True)
    tag_barcode = Column(String(100), unique=True)
    member_size = Column(String(100))
    drop_length_display = Column(String(50))
    drop_length_inches = Column(Float)
    weight = Column(Numeric(10, 2))
    grade = Column(String(50))
    heat_number = Column(String(100))
    source_po = Column(String(50))
    disposition = Column(String(20))  # NULL, 'inventory', 'scrap'
    disposition_date = Column(DateTime)
    disposition_by = Column(String(100))
    inventory_location = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── MATERIAL INVENTORY ──────────────────────────────────────

class MaterialInventory(Base):
    __tablename__ = "tracker_material_inventory"
    id = Column(Integer, primary_key=True)
    barcode = Column(String(50), unique=True, nullable=True)
    source_type = Column(String(20))  # drop, surplus, purchased, manual
    drop_tag_id = Column(Integer, ForeignKey("tracker_drop_tags.id"), nullable=True)
    member_size = Column(String(100))
    shape = Column(String(100))
    dimensions = Column(String(100))
    length_display = Column(String(50))
    length_inches = Column(Float)
    width_inches = Column(Float, nullable=True)
    quantity = Column(Integer, default=1)
    weight = Column(Numeric(10, 2))
    grade = Column(String(50))
    heat_number = Column(String(100))
    location = Column(String(100))
    status = Column(String(20), default="available")  # available, reserved, used, scrapped
    reserved_project_id = Column(Integer, ForeignKey("tracker_projects.id"), nullable=True)
    reserved_date = Column(DateTime, nullable=True)
    reserved_by = Column(String(100), nullable=True)
    added_date = Column(DateTime, default=datetime.utcnow)
    added_by = Column(String(100))
    notes = Column(Text)


# ─── DOCUMENT PACKETS ────────────────────────────────────────

class DocumentPacket(Base):
    __tablename__ = "tracker_document_packets"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("tracker_projects.id"))
    doc_type = Column(String(20))  # transmittal, change_order, rfi
    doc_number = Column(String(50))
    to_company = Column(String(200))
    to_contact = Column(String(200))
    to_address = Column(Text)
    to_phone = Column(String(50))
    to_fax = Column(String(50))
    to_email = Column(String(200))
    subject = Column(String(500))
    description = Column(Text)
    # Change order specific
    co_shop_drawings = Column(Numeric(10, 2), default=0)
    co_material = Column(Numeric(10, 2), default=0)
    co_fabrication = Column(Numeric(10, 2), default=0)
    co_coating = Column(Numeric(10, 2), default=0)
    co_field_work = Column(Numeric(10, 2), default=0)
    co_overhead_pct = Column(Numeric(5, 2), default=15)
    co_total = Column(Numeric(12, 2), default=0)
    # Transmittal specific
    transmittal_items = Column(JSON)  # list of drawing numbers, revisions, descriptions
    prints_enclosed = Column(Integer, default=0)
    # Status
    attachment_count = Column(Integer, default=0)
    status = Column(String(20), default="draft")  # draft, sent, returned, approved, rejected
    date_sent = Column(DateTime)
    date_due = Column(DateTime)
    response_status = Column(String(20))
    response_date = Column(DateTime)
    response_notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Relationships
    attachments = relationship("PacketAttachment", back_populates="packet", cascade="all, delete-orphan")


class PacketAttachment(Base):
    __tablename__ = "tracker_packet_attachments"
    id = Column(Integer, primary_key=True)
    packet_id = Column(Integer, ForeignKey("tracker_document_packets.id"))
    filename = Column(String(500))
    file_data = Column(Text)  # base64 encoded
    file_size = Column(Integer)
    upload_date = Column(DateTime, default=datetime.utcnow)
    sort_order = Column(Integer, default=0)
    packet = relationship("DocumentPacket", back_populates="attachments")


def generate_barcode():
    """Generate a unique barcode string"""
    return f"SSE-{uuid.uuid4().hex[:8].upper()}"
