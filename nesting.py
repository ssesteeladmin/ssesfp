"""
SSE Steel - Nesting / Cutting Stock Optimization Engine
1D bin packing for linear members (beams, HSS, angles, channels, pipe, etc.)
2D nesting for plates (simplified rectangular nesting)
"""
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
import math


# ─── STANDARD STOCK LENGTHS BY SHAPE TYPE ────────────────

DEFAULT_STOCK_LENGTHS = {
    # Shape type: list of available stock lengths in FEET
    "W": [20, 25, 30, 35, 40, 45, 50, 55, 60],
    "S": [20, 25, 30, 35, 40],
    "HSS": [20, 24, 40, 48],
    "HSSR": [20, 24, 40, 48],  # HSS Rectangular
    "TS": [20, 24, 40, 48],    # Tube Steel (same as HSS)
    "L": [20, 25, 30, 40],     # Angles
    "C": [20, 25, 30, 40],     # Channels
    "MC": [20, 25, 30, 40],    # MC Channels
    "PIPE": [21, 42],
    "PL": [],                    # Plates - special handling (sheets)
    "FB": [20, 25, 30, 40],    # Flat bar
    "ROD": [20, 25, 30, 40],   # Round bar / rod
    "MB": [20],                  # Misc bar
}

# Standard plate sizes (width x length in inches)
DEFAULT_PLATE_SIZES = [
    (48, 96),    # 4' x 8'
    (48, 120),   # 4' x 10'
    (48, 144),   # 4' x 12'
    (48, 240),   # 4' x 20'
    (60, 96),    # 5' x 8'
    (60, 120),   # 5' x 10'
    (60, 144),   # 5' x 12'
    (60, 240),   # 5' x 20'
    (72, 96),    # 6' x 8'
    (72, 120),   # 6' x 10'
    (72, 144),   # 6' x 12'
    (72, 240),   # 6' x 20'
    (96, 96),    # 8' x 8'
    (96, 120),   # 8' x 10'
    (96, 144),   # 8' x 12'
    (96, 240),   # 8' x 20'
    (96, 288),   # 8' x 24'
    (96, 480),   # 8' x 40'
    (120, 240),  # 10' x 20'
    (120, 480),  # 10' x 40'
]

KERF = 0.25  # inches - saw blade kerf allowance


@dataclass
class CutPiece:
    """A piece that needs to be cut from stock."""
    part_mark: str
    assembly_mark: str
    shape: str
    dimensions: str
    grade: str
    length_inches: float
    quantity: int
    weight_per_foot: float = 0
    project_id: int = 0


@dataclass
class StockBar:
    """A stock bar that pieces are cut from."""
    stock_length_inches: float
    shape: str
    dimensions: str
    grade: str
    cuts: List[CutPiece] = field(default_factory=list)
    remaining_inches: float = 0
    from_inventory: bool = False
    inventory_id: int = 0
    
    def __post_init__(self):
        self.remaining_inches = self.stock_length_inches

    @property
    def used_inches(self):
        return sum(c.length_inches + KERF for c in self.cuts) - (KERF if self.cuts else 0)
    
    @property
    def waste_inches(self):
        return self.stock_length_inches - self.used_inches
    
    @property
    def utilization(self):
        if self.stock_length_inches == 0:
            return 0
        return round(self.used_inches / self.stock_length_inches * 100, 1)


@dataclass
class NestResult:
    """Complete nesting result for a project."""
    bars: List[StockBar]
    unplaced: List[CutPiece]
    summary: Dict
    

def get_shape_category(shape: str) -> str:
    """Map specific shapes to their stock length category."""
    shape = shape.upper().strip()
    if shape in ("W", "S"):
        return shape
    if shape in ("HSS", "HSSR", "TS"):
        return "HSS"
    if shape == "L":
        return "L"
    if shape in ("C", "MC"):
        return shape
    if shape == "PIPE":
        return "PIPE"
    if shape == "PL":
        return "PL"
    if shape in ("FB", "FLAT"):
        return "FB"
    if shape in ("ROD", "RD"):
        return "ROD"
    return shape


def nest_linear(
    pieces: List[CutPiece],
    stock_lengths: Dict[str, List[float]] = None,
    inventory: List[Dict] = None,
    kerf: float = KERF,
) -> NestResult:
    """
    1D cutting stock optimization using First Fit Decreasing algorithm.
    
    Args:
        pieces: List of pieces that need to be cut
        stock_lengths: Dict of shape -> available stock lengths in FEET
        inventory: List of inventory items to use first
        kerf: Saw blade kerf in inches
        
    Returns:
        NestResult with optimized bar assignments
    """
    if stock_lengths is None:
        stock_lengths = DEFAULT_STOCK_LENGTHS
    if inventory is None:
        inventory = []
    
    # Expand pieces by quantity
    expanded = []
    for p in pieces:
        for i in range(p.quantity):
            expanded.append(CutPiece(
                part_mark=p.part_mark,
                assembly_mark=p.assembly_mark,
                shape=p.shape,
                dimensions=p.dimensions,
                grade=p.grade,
                length_inches=p.length_inches,
                quantity=1,
                weight_per_foot=p.weight_per_foot,
                project_id=p.project_id,
            ))
    
    # Group by shape + dimensions + grade (must nest within same material)
    groups = {}
    for p in expanded:
        key = f"{p.shape}|{p.dimensions}|{p.grade}"
        if key not in groups:
            groups[key] = []
        groups[key].append(p)
    
    all_bars = []
    all_unplaced = []
    
    for key, group_pieces in groups.items():
        shape, dimensions, grade = key.split("|")
        category = get_shape_category(shape)
        
        # Skip plates - handled separately
        if category == "PL":
            continue
        
        # Sort pieces longest first (FFD)
        group_pieces.sort(key=lambda p: p.length_inches, reverse=True)
        
        # Get available stock lengths for this shape (in inches)
        available_ft = stock_lengths.get(category, stock_lengths.get(shape, [20, 40]))
        available_inches = sorted([ft * 12 for ft in available_ft], reverse=True)
        
        if not available_inches:
            all_unplaced.extend(group_pieces)
            continue
        
        # Check inventory first
        inv_bars = []
        for inv_item in inventory:
            if (inv_item.get('shape', '').upper() == shape.upper() and
                inv_item.get('dimensions', '').upper() == dimensions.upper() and
                inv_item.get('grade', '').upper() == grade.upper() and
                inv_item.get('quantity', 0) > 0):
                for _ in range(inv_item['quantity']):
                    bar = StockBar(
                        stock_length_inches=inv_item.get('length_inches', 240),
                        shape=shape,
                        dimensions=dimensions,
                        grade=grade,
                        from_inventory=True,
                        inventory_id=inv_item.get('id', 0),
                    )
                    inv_bars.append(bar)
        
        # FFD bin packing
        bars = list(inv_bars)  # Start with inventory bars
        
        for piece in group_pieces:
            placed = False
            
            # Try to fit in existing bars (best fit - smallest remaining that works)
            best_bar = None
            best_remaining = float('inf')
            
            for bar in bars:
                space_needed = piece.length_inches + (kerf if bar.cuts else 0)
                new_remaining = bar.remaining_inches - space_needed
                if new_remaining >= 0 and new_remaining < best_remaining:
                    best_bar = bar
                    best_remaining = new_remaining
            
            if best_bar is not None:
                space_needed = piece.length_inches + (kerf if best_bar.cuts else 0)
                best_bar.remaining_inches -= space_needed
                best_bar.cuts.append(piece)
                placed = True
            
            if not placed:
                # Need a new bar - pick smallest stock that fits
                for stock_len in sorted(available_inches):
                    if stock_len >= piece.length_inches:
                        new_bar = StockBar(
                            stock_length_inches=stock_len,
                            shape=shape,
                            dimensions=dimensions,
                            grade=grade,
                        )
                        new_bar.remaining_inches -= piece.length_inches
                        new_bar.cuts.append(piece)
                        bars.append(new_bar)
                        placed = True
                        break
            
            if not placed:
                all_unplaced.append(piece)
        
        all_bars.extend(bars)
    
    # Generate summary
    total_stock_feet = sum(b.stock_length_inches / 12 for b in all_bars)
    total_used_feet = sum(b.used_inches / 12 for b in all_bars)
    total_waste_feet = total_stock_feet - total_used_feet
    from_inventory_count = sum(1 for b in all_bars if b.from_inventory)
    to_purchase_count = sum(1 for b in all_bars if not b.from_inventory)
    
    # Group purchase list by material
    purchase_list = {}
    for bar in all_bars:
        if not bar.from_inventory:
            pkey = f"{bar.shape}|{bar.dimensions}|{bar.grade}|{bar.stock_length_inches}"
            if pkey not in purchase_list:
                purchase_list[pkey] = {
                    "shape": bar.shape,
                    "dimensions": bar.dimensions,
                    "grade": bar.grade,
                    "stock_length_ft": bar.stock_length_inches / 12,
                    "quantity": 0,
                }
            purchase_list[pkey]["quantity"] += 1
    
    summary = {
        "total_bars": len(all_bars),
        "from_inventory": from_inventory_count,
        "to_purchase": to_purchase_count,
        "total_stock_feet": round(total_stock_feet, 1),
        "total_used_feet": round(total_used_feet, 1),
        "total_waste_feet": round(total_waste_feet, 1),
        "overall_utilization": round((total_used_feet / total_stock_feet * 100) if total_stock_feet > 0 else 0, 1),
        "unplaced_pieces": len(all_unplaced),
        "purchase_list": list(purchase_list.values()),
    }
    
    return NestResult(bars=all_bars, unplaced=all_unplaced, summary=summary)


def nest_plates(
    pieces: List[Dict],
    plate_sizes: List[Tuple[float, float]] = None,
) -> Dict:
    """
    Simplified rectangular plate nesting.
    Groups plate cuts by thickness/grade and assigns to standard sheets.
    """
    if plate_sizes is None:
        plate_sizes = DEFAULT_PLATE_SIZES
    
    # Group by thickness (from dimensions like "PL 1/2" or "PL3/4")
    groups = {}
    for p in pieces:
        key = f"{p['dimensions']}|{p['grade']}"
        if key not in groups:
            groups[key] = []
        groups[key].append(p)
    
    results = []
    for key, group in groups.items():
        dims, grade = key.split("|")
        # For plates, we track total area needed
        total_area = sum(
            p.get('width_inches', 12) * p.get('length_inches', 12) * p.get('quantity', 1)
            for p in group
        )
        
        # Find best fitting plate size
        best_sheets = []
        remaining_area = total_area
        
        for pw, pl in sorted(plate_sizes, key=lambda s: s[0] * s[1], reverse=True):
            sheet_area = pw * pl
            while remaining_area > 0:
                best_sheets.append({"width": pw, "length": pl, "area": sheet_area})
                remaining_area -= sheet_area * 0.85  # 85% utilization estimate
                if remaining_area <= 0:
                    break
            if remaining_area <= 0:
                break
        
        results.append({
            "dimensions": dims,
            "grade": grade,
            "total_area_sqft": round(total_area / 144, 1),
            "pieces": len(group),
            "sheets_needed": len(best_sheets),
            "sheets": best_sheets,
        })
    
    return results


def generate_rfq(nest_result: NestResult, project_name: str = "") -> List[Dict]:
    """Generate RFQ line items from nesting purchase list."""
    rfq_items = []
    for item in nest_result.summary.get("purchase_list", []):
        rfq_items.append({
            "shape": item["shape"],
            "dimensions": item["dimensions"],
            "grade": item["grade"],
            "length_ft": item["stock_length_ft"],
            "quantity": item["quantity"],
            "total_feet": item["stock_length_ft"] * item["quantity"],
            "description": f'{item["shape"]} {item["dimensions"]} x {item["stock_length_ft"]}\' - {item["grade"]}',
            "project": project_name,
        })
    return rfq_items
