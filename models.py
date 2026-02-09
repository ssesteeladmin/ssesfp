"""
SSE Steel Project Tracker - Database Models
Shared Neon PostgreSQL database with production dashboard
"""
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Text, DateTime, Date,
    ForeignKey, JSON, Enum as SQLEnum, Index, UniqueConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
import enum

Base = declarative_base()

# ─── ENUMS ───────────────────────────────────────────────

class FinishType(str, enum.Enum):
    ROP = "ROP"
    TESLA_GREY = "Tesla Grey"
    TESLA_WHITE = "Tesla White"
    GALVANIZE = "Galvanize"
    THREE_COAT = "3 Coat System"
    COAL_TAR = "Coal Tar"
    NONE = "None"

class DrawingStatus(str, enum.Enum):
    IFA_A = "A"  # Issue for Approval
    IFA_B = "B"
    IFA_C = "C"
    IFA_D = "D"
    IFC_0 = "0"  # Issued for Fabrication
    IFC_1 = "1"
    IFC_2 = "2"
    IFC_3 = "3"
    IFC_4 = "4"
    IFC_5 = "5"

class StationName(str, enum.Enum):
    DETAILING = "Detailing"
    RAW_MATERIAL = "Raw Material"
    SAW = "Saw"
    PYTHON_BEAM = "Python Beam"
    PYTHON_PLATE = "Python Plate"
    LASER = "Laser"
    EMI = "EMI"
    FIT = "Fit"
    QC_FIT = "QC - Fit Inspection"
    WELD = "Weld"
    QC_WELD = "QC - Weld Inspection"
    FINISH = "Finish"
    PAINT_OUT = "Paint - Sent Out"
    PAINT_RECEIVED = "Paint - Received"
    GALV_OUT = "Galvanize - Sent Out"
    GALV_RECEIVED = "Galvanize - Received"
    READY_TO_SHIP = "Ready to Ship"
    LOADED = "Loaded"
    SHIPPED = "Shipped"
    SHIPPED_FROM_GALV = "Shipped from Galvanizer"

class InspectionType(str, enum.Enum):
    FIT_UP = "Fit-Up Inspection"
    VISUAL_WELD = "Visual Weld Inspection"
    FINAL = "Final Inspection"
    BOLT = "Bolt Inspection"
    NDT = "NDT Inspection"
    COATING = "Coating Inspection"

class InspectionResult(str, enum.Enum):
    PASS = "Pass"
    FAIL = "Fail"
    HOLD = "Hold"
    REWORK = "Rework Required"

class ContactType(str, enum.Enum):
    CUSTOMER = "Customer"
    GC = "General Contractor"
    ENGINEER = "Engineer"
    PAINTER = "Painter"
    GALVANIZER = "Galvanizer"
    ERECTOR = "Erector"
    DETAILER = "Detailer"
    VENDOR = "Vendor"

# ─── ADDRESS BOOK ────────────────────────────────────────

class Company(Base):
    __tablename__ = "tracker_companies"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    company_type = Column(String(50))  # ContactType values
    address_line1 = Column(String(255))
    address_line2 = Column(String(255))
    city = Column(String(100))
    state = Column(String(50))
    zip_code = Column(String(20))
    phone = Column(String(50))
    fax = Column(String(50))
    email = Column(String(255))
    website = Column(String(255))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    contacts = relationship("Contact", back_populates="company", cascade="all, delete-orphan")

class Contact(Base):
    __tablename__ = "tracker_contacts"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("tracker_companies.id"))
    name = Column(String(255), nullable=False)
    title = Column(String(100))
    phone = Column(String(50))
    cell = Column(String(50))
    email = Column(String(255))
    is_primary = Column(Boolean, default=False)
    notes = Column(Text)
    company = relationship("Company", back_populates="contacts")

# ─── PROJECTS ────────────────────────────────────────────

class Project(Base):
    __tablename__ = "tracker_projects"
    id = Column(Integer, primary_key=True)
    job_number = Column(String(20), unique=True, nullable=False)  # e.g. 26-1016
    project_name = Column(String(500), nullable=False)
    customer_id = Column(Integer, ForeignKey("tracker_companies.id"))
    gc_id = Column(Integer, ForeignKey("tracker_companies.id"))
    engineer_id = Column(Integer, ForeignKey("tracker_companies.id"))
    detailer_id = Column(Integer, ForeignKey("tracker_companies.id"))
    painter_id = Column(Integer, ForeignKey("tracker_companies.id"))
    galvanizer_id = Column(Integer, ForeignKey("tracker_companies.id"))
    erector_id = Column(Integer, ForeignKey("tracker_companies.id"))
    finish_type = Column(String(50), default="None")
    contract_weight = Column(Float, default=0)
    contract_amount = Column(Float, default=0)
    po_number = Column(String(100))
    ship_to_address = Column(Text)
    notes = Column(Text)
    status = Column(String(50), default="Active")  # Active, Complete, Hold, Cancelled
    archived = Column(Boolean, default=False)
    project_manager = Column(String(200))
    start_date = Column(Date)
    due_date = Column(Date)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Relationships
    assemblies = relationship("Assembly", back_populates="project", cascade="all, delete-orphan")
    drawings = relationship("Drawing", back_populates="project", cascade="all, delete-orphan")
    shipments = relationship("Shipment", back_populates="project", cascade="all, delete-orphan")
    project_contacts = relationship("ProjectContact", back_populates="project", cascade="all, delete-orphan")

class ProjectContact(Base):
    __tablename__ = "tracker_project_contacts"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    contact_id = Column(Integer, ForeignKey("tracker_contacts.id"))
    role = Column(String(100))  # PM, Superintendent, Inspector, etc.
    project = relationship("Project", back_populates="project_contacts")

# ─── DRAWINGS ────────────────────────────────────────────

class Drawing(Base):
    __tablename__ = "tracker_drawings"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    drawing_number = Column(String(100), nullable=False)
    drawing_title = Column(String(500))
    category = Column(String(50))  # General Arrangement, Assembly, Single Part
    current_revision = Column(String(10), default="0")
    revision_status = Column(String(20), default="IFC")  # IFA or IFC
    date_detailed = Column(Date)
    date_revised = Column(Date)
    revision_description = Column(String(255))
    model_ref = Column(String(100))
    pdf_data = Column(Text)  # base64 encoded PDF
    created_at = Column(DateTime, default=datetime.utcnow)
    project = relationship("Project", back_populates="drawings")

class DrawingRevision(Base):
    __tablename__ = "tracker_drawing_revisions"
    id = Column(Integer, primary_key=True)
    drawing_id = Column(Integer, ForeignKey("tracker_drawings.id"))
    revision_number = Column(String(10))
    revision_description = Column(String(255))
    date_revised = Column(Date)
    pdf_data = Column(Text)  # base64 encoded PDF for this revision
    created_at = Column(DateTime, default=datetime.utcnow)

# ─── ASSEMBLIES & PARTS ─────────────────────────────────

class Assembly(Base):
    __tablename__ = "tracker_assemblies"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    assembly_id_tekla = Column(String(100))  # UUID from Tekla
    model_ref = Column(String(100))
    assembly_mark = Column(String(100), nullable=False)  # e.g. 25-1019-B181
    assembly_name = Column(String(100))  # BEAM, COLUMN, etc.
    assembly_quantity = Column(Integer, default=1)
    assembly_length_mm = Column(Float)
    assembly_weight = Column(Float)  # lbs
    drawing_number = Column(String(100))
    sequence_number = Column(Integer)
    sequence_lot_qty = Column(Integer)
    finish_type = Column(String(50))
    current_station = Column(String(100), default="Detailing")
    route = Column(String(500))  # comma-separated station route
    qr_code_data = Column(Text)  # QR code content string
    barcode_printed = Column(Boolean, default=False)
    barcode_print_date = Column(DateTime)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Relationships
    project = relationship("Project", back_populates="assemblies")
    parts = relationship("Part", back_populates="assembly", cascade="all, delete-orphan")
    scans = relationship("ScanEvent", back_populates="assembly", cascade="all, delete-orphan")
    inspections = relationship("Inspection", back_populates="assembly", cascade="all, delete-orphan")
    __table_args__ = (
        Index("idx_assembly_project_mark", "project_id", "assembly_mark"),
    )

class Part(Base):
    __tablename__ = "tracker_parts"
    id = Column(Integer, primary_key=True)
    assembly_id = Column(Integer, ForeignKey("tracker_assemblies.id"))
    part_id_tekla = Column(String(100))
    model_ref = Column(String(100))
    part_mark = Column(String(100))  # e.g. s1016, an1189
    is_main_member = Column(Boolean, default=False)
    quantity = Column(Integer, default=1)
    shape = Column(String(20))  # L, PL, S, HSS, W, ROD, HS, NU, WA, MB, etc.
    dimensions = Column(String(100))  # e.g. S8X18.4, L4X3-1/2X5/16
    grade = Column(String(50))  # A36, A992, A325TC, etc.
    length_inches = Column(Float)
    length_display = Column(String(50))  # e.g. 6'-10"
    weight = Column(Float)
    is_hardware = Column(Boolean, default=False)  # bolts, nuts, washers
    remark = Column(String(255))  # Field, Shop, etc.
    pay_category = Column(String(100))
    cnc_file = Column(String(255))  # reference to NC1 file
    notes = Column(Text)
    assembly = relationship("Assembly", back_populates="parts")

# ─── SCAN EVENTS (Station Tracking) ─────────────────────

class ScanEvent(Base):
    __tablename__ = "tracker_scan_events"
    id = Column(Integer, primary_key=True)
    assembly_id = Column(Integer, ForeignKey("tracker_assemblies.id"))
    station = Column(String(100), nullable=False)
    scanned_by = Column(String(100))
    scanned_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)
    device_info = Column(String(255))  # phone/tablet identifier
    gps_lat = Column(Float)  # optional GPS for yard tracking
    gps_lon = Column(Float)
    assembly = relationship("Assembly", back_populates="scans")
    __table_args__ = (
        Index("idx_scan_assembly", "assembly_id"),
        Index("idx_scan_station", "station"),
        Index("idx_scan_date", "scanned_at"),
    )

# ─── QC INSPECTIONS (AISC Compliant) ────────────────────

class Inspection(Base):
    __tablename__ = "tracker_inspections"
    id = Column(Integer, primary_key=True)
    assembly_id = Column(Integer, ForeignKey("tracker_assemblies.id"))
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    inspection_type = Column(String(50), nullable=False)  # InspectionType values
    result = Column(String(50))  # InspectionResult values
    inspector = Column(String(100))
    inspection_date = Column(DateTime, default=datetime.utcnow)
    wps_number = Column(String(50))  # Welding Procedure Spec
    welder_id = Column(String(50))  # Welder stencil/ID
    ndt_method = Column(String(50))  # VT, MT, UT, RT, PT
    ndt_report_number = Column(String(100))
    checklist_data = Column(JSON)  # Flexible checklist items
    findings = Column(Text)
    corrective_action = Column(Text)
    retest_required = Column(Boolean, default=False)
    retest_date = Column(DateTime)
    retest_result = Column(String(50))
    photos = Column(JSON)  # base64 photo references
    signed_off = Column(Boolean, default=False)
    signed_off_by = Column(String(100))
    signed_off_date = Column(DateTime)
    notes = Column(Text)
    assembly = relationship("Assembly", back_populates="inspections")
    __table_args__ = (
        Index("idx_inspection_project", "project_id"),
        Index("idx_inspection_type", "inspection_type"),
    )

# ─── SHIPPING ────────────────────────────────────────────

class Shipment(Base):
    __tablename__ = "tracker_shipments"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    load_number = Column(Integer)
    truck_number = Column(String(50))
    trailer_type = Column(String(50))  # Flatbed, Step Deck, Lowboy
    carrier = Column(String(255))
    driver_name = Column(String(100))
    destination = Column(String(50))  # customer, galvanizer, painter
    destination_company_id = Column(Integer, ForeignKey("tracker_companies.id"))
    ship_date = Column(DateTime)
    estimated_arrival = Column(DateTime)
    actual_arrival = Column(DateTime)
    total_weight = Column(Float, default=0)
    total_pieces = Column(Integer, default=0)
    bill_of_lading = Column(String(100))
    notes = Column(Text)
    status = Column(String(50), default="Loading")  # Loading, In Transit, Delivered
    created_at = Column(DateTime, default=datetime.utcnow)
    project = relationship("Project", back_populates="shipments")
    items = relationship("ShipmentItem", back_populates="shipment", cascade="all, delete-orphan")

class ShipmentItem(Base):
    __tablename__ = "tracker_shipment_items"
    id = Column(Integer, primary_key=True)
    shipment_id = Column(Integer, ForeignKey("tracker_shipments.id"))
    assembly_id = Column(Integer, ForeignKey("tracker_assemblies.id"))
    scanned_at = Column(DateTime, default=datetime.utcnow)
    scanned_by = Column(String(100))
    position_on_truck = Column(String(50))  # layer, position
    shipment = relationship("Shipment", back_populates="items")

# ─── MATERIAL / PO TRACKING ─────────────────────────────

class PurchaseOrder(Base):
    __tablename__ = "tracker_purchase_orders"
    id = Column(Integer, primary_key=True)
    po_number = Column(String(50), unique=True)
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    vendor_id = Column(Integer, ForeignKey("tracker_companies.id"))
    status = Column(String(50), default="Draft")  # Draft, Sent, Partial, Received, Complete
    order_date = Column(Date)
    expected_date = Column(Date)
    received_date = Column(Date)
    total_amount = Column(Float, default=0)
    total_weight = Column(Float, default=0)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    items = relationship("POItem", back_populates="purchase_order", cascade="all, delete-orphan")

class POItem(Base):
    __tablename__ = "tracker_po_items"
    id = Column(Integer, primary_key=True)
    po_id = Column(Integer, ForeignKey("tracker_purchase_orders.id"))
    shape = Column(String(20))
    dimensions = Column(String(100))
    grade = Column(String(50))
    length_inches = Column(Float)
    quantity = Column(Integer, default=1)
    weight_each = Column(Float)
    weight_total = Column(Float)
    unit_price = Column(Float)
    total_price = Column(Float)
    received_qty = Column(Integer, default=0)
    heat_number = Column(String(100))  # AISC traceability
    mill_cert = Column(Text)  # base64 mill cert PDF
    notes = Column(Text)
    purchase_order = relationship("PurchaseOrder", back_populates="items")

# ─── AISC AUDIT TRAIL ───────────────────────────────────

class AuditLog(Base):
    __tablename__ = "tracker_audit_log"
    id = Column(Integer, primary_key=True)
    table_name = Column(String(100))
    record_id = Column(Integer)
    action = Column(String(20))  # CREATE, UPDATE, DELETE
    field_name = Column(String(100))
    old_value = Column(Text)
    new_value = Column(Text)
    user = Column(String(100))
    timestamp = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_audit_table_record", "table_name", "record_id"),
    )

# ─── INVENTORY ───────────────────────────────────────────

class Inventory(Base):
    __tablename__ = "tracker_inventory"
    id = Column(Integer, primary_key=True)
    shape = Column(String(20), nullable=False)
    dimensions = Column(String(100), nullable=False)
    grade = Column(String(50), nullable=False)
    length_inches = Column(Float)
    length_display = Column(String(50))
    quantity = Column(Integer, default=1)
    location = Column(String(100))  # Rack, Bay, Yard location
    heat_number = Column(String(100))
    po_reference = Column(String(100))
    date_received = Column(Date)
    reserved_for_project = Column(Integer, ForeignKey("tracker_projects.id"), nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

# ─── STOCK LENGTH CONFIG ─────────────────────────────────

class StockLengthConfig(Base):
    __tablename__ = "tracker_stock_lengths"
    id = Column(Integer, primary_key=True)
    shape_category = Column(String(20), nullable=False)  # W, HSS, L, C, PL, etc
    length_feet = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)
    notes = Column(String(255))

# ─── RFQ (Request for Quote) ─────────────────────────────

class RFQ(Base):
    __tablename__ = "tracker_rfqs"
    id = Column(Integer, primary_key=True)
    rfq_number = Column(String(50), unique=True)
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    status = Column(String(50), default="Draft")  # Draft, Sent, Quoted, Ordered
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_date = Column(Date)
    due_date = Column(Date)
    notes = Column(Text)
    items = relationship("RFQItem", back_populates="rfq", cascade="all, delete-orphan")

class RFQItem(Base):
    __tablename__ = "tracker_rfq_items"
    id = Column(Integer, primary_key=True)
    rfq_id = Column(Integer, ForeignKey("tracker_rfqs.id"))
    shape = Column(String(20))
    dimensions = Column(String(100))
    grade = Column(String(50))
    length_feet = Column(Float)
    quantity = Column(Integer, default=1)
    total_feet = Column(Float)
    description = Column(String(500))
    quoted_price = Column(Float)
    quoted_by = Column(String(255))
    selected = Column(Boolean, default=False)
    rfq = relationship("RFQ", back_populates="items")

# ─── TRANSMITTALS ────────────────────────────────────────

class Transmittal(Base):
    __tablename__ = "tracker_transmittals"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    transmittal_number = Column(String(50))
    to_company_id = Column(Integer, ForeignKey("tracker_companies.id"))
    to_contact = Column(String(255))
    to_email = Column(String(255))
    from_name = Column(String(255), default="SSE Steel")
    subject = Column(String(500))
    message = Column(Text)
    items_description = Column(Text)  # What's being transmitted
    drawing_numbers = Column(Text)    # Comma-separated drawing numbers
    action_required = Column(String(100))  # For Review, For Approval, For Construction, For Record
    status = Column(String(50), default="Draft")  # Draft, Sent, Acknowledged
    sent_date = Column(DateTime)
    acknowledged_date = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

# ─── RFI (Request for Information) ───────────────────────

class RFI(Base):
    __tablename__ = "tracker_rfis"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    rfi_number = Column(String(50))
    subject = Column(String(500))
    question = Column(Text, nullable=False)
    response = Column(Text)
    drawing_reference = Column(String(255))
    detail_reference = Column(String(255))
    spec_reference = Column(String(255))
    submitted_by = Column(String(255), default="SSE Steel")
    submitted_to = Column(String(255))
    to_company_id = Column(Integer, ForeignKey("tracker_companies.id"))
    to_email = Column(String(255))
    priority = Column(String(50), default="Normal")  # Low, Normal, High, Urgent
    status = Column(String(50), default="Draft")  # Draft, Sent, Responded, Closed
    date_submitted = Column(DateTime)
    date_required = Column(Date)
    date_responded = Column(DateTime)
    impact_cost = Column(Boolean, default=False)
    impact_schedule = Column(Boolean, default=False)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

# ─── CHANGE ORDERS ───────────────────────────────────────

class ChangeOrder(Base):
    __tablename__ = "tracker_change_orders"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("tracker_projects.id"))
    co_number = Column(String(50))
    title = Column(String(500))
    description = Column(Text)
    reason = Column(String(255))  # Design Change, Field Condition, Owner Request, Error/Omission
    drawing_references = Column(Text)
    rfi_reference = Column(String(50))  # Related RFI number
    cost_impact = Column(Float, default=0)
    schedule_impact_days = Column(Integer, default=0)
    weight_change_lbs = Column(Float, default=0)
    status = Column(String(50), default="Draft")  # Draft, Submitted, Approved, Rejected, Completed
    submitted_date = Column(Date)
    approved_date = Column(Date)
    approved_by = Column(String(255))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─── GENERIC DOCUMENT ATTACHMENTS ────────────────────────

class DocAttachment(Base):
    """Generic file attachment for transmittals, RFIs, change orders."""
    __tablename__ = "tracker_doc_attachments"
    id = Column(Integer, primary_key=True)
    parent_type = Column(String(20), nullable=False)  # transmittal, rfi, change_order
    parent_id = Column(Integer, nullable=False)
    filename = Column(String(500))
    file_data = Column(Text)  # base64 encoded
    file_size = Column(Integer, default=0)
    file_type = Column(String(50))  # pdf, jpg, png, dwg, etc.
    is_drawing = Column(Boolean, default=False)  # if this is a project drawing attachment
    drawing_id = Column(Integer, ForeignKey("tracker_drawings.id"), nullable=True)
    sort_order = Column(Integer, default=0)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("idx_doc_attachment_parent", "parent_type", "parent_id"),
    )
