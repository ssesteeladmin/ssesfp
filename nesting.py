"""
SSE Steel - Nesting Engine & AISC Shape Database
1D Cutting Stock Optimization with inventory cross-reference
"""
from typing import List, Dict, Optional
from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════
#  STANDARD STOCK LENGTHS BY SHAPE (in feet)
# ═══════════════════════════════════════════════════════════

STOCK_LENGTHS = {
    "W":    [20, 25, 30, 35, 40, 45, 50, 55, 60],
    "S":    [20, 25, 30, 35, 40],
    "HSS":  [20, 24, 40, 48],
    "HSSR": [20, 24, 40, 48],
    "RT":   [20, 24, 40, 48],
    "TS":   [20, 24, 40, 48],
    "PIPE": [21, 42],
    "L":    [20, 25, 30, 40],
    "C":    [20, 25, 30, 40, 60],
    "MC":   [20, 25, 30, 40, 60],
    "PL":   [8, 10, 12, 16, 20, 24, 40],
    "FB":   [20, 24],
    "WT":   [20, 25, 30, 35, 40],
    "HP":   [20, 40, 60],
    "ROD":  [12, 20],
    "BPL":  [8, 10, 12, 16, 20],
    "MB":   [20, 25, 30, 40],
}

STANDARD_GRADES = {
    "W":    ["A992", "A36", "A572-50"],
    "S":    ["A36", "A992"],
    "HSS":  ["A500-GR.B", "A500-GR.C", "A500-GR.46"],
    "HSSR": ["A500-GR.B", "A500-GR.C"],
    "RT":   ["A500-GR.B", "A500-GR.C"],
    "TS":   ["A500-GR.B", "A500-GR.C"],
    "PIPE": ["A53-GR.B", "A500-GR.B"],
    "L":    ["A36", "A572-50"],
    "C":    ["A36", "A572-50"],
    "MC":   ["A36"],
    "PL":   ["A36", "A572-50", "A588"],
    "FB":   ["A36"],
    "WT":   ["A992", "A36"],
    "HP":   ["A572-50", "A36"],
    "ROD":  ["A36"],
    "BPL":  ["A36"],
    "MB":   ["A36"],
}

KERF = 0.25  # saw blade kerf in inches


@dataclass
class CutPiece:
    part_mark: str
    assembly_mark: str
    length_inches: float
    quantity: int
    shape: str
    dimensions: str
    grade: str


@dataclass
class StockBar:
    stock_length_ft: float
    shape: str
    dimensions: str
    grade: str
    cuts: List[CutPiece] = field(default_factory=list)
    used_inches: float = 0
    from_inventory: bool = False
    inventory_id: int = 0

    @property
    def stock_length_in(self):
        return self.stock_length_ft * 12

    @property
    def remaining(self):
        return self.stock_length_in - self.used_inches

    @property
    def waste_inches(self):
        return self.stock_length_in - self.used_inches

    @property
    def utilization(self):
        return round(self.used_inches / self.stock_length_in * 100, 1) if self.stock_length_in else 0

    def can_fit(self, length_inches):
        needed = length_inches + (KERF if self.cuts else 0)
        return self.remaining >= needed

    def add_cut(self, piece):
        if self.cuts:
            self.used_inches += KERF
        self.used_inches += piece.length_inches
        self.cuts.append(piece)


def get_shape_category(shape: str) -> str:
    s = shape.upper().strip()
    for prefix in ["HSS", "HSSR", "RT", "TS", "PIPE", "MC", "BPL", "PL", "FB", "WT", "HP", "ROD", "MB", "W", "S", "L", "C"]:
        if s.startswith(prefix):
            return prefix
    return s


def get_stock_lengths_for(shape: str, overrides=None) -> List[int]:
    cat = get_shape_category(shape)
    if overrides and cat in overrides:
        return sorted(overrides[cat])
    return sorted(STOCK_LENGTHS.get(cat, [20, 40]))


def nest_group(
    pieces: List[CutPiece],
    stock_lengths_ft: List[int],
    inventory: List[Dict] = None,
) -> Dict:
    """
    1D cutting stock optimization using Best Fit Decreasing.
    Groups pieces by material, optimizes cuts, cross-references inventory.
    """
    if not pieces:
        return {"bars": [], "summary": {}}

    shape = pieces[0].shape
    dimensions = pieces[0].dimensions
    grade = pieces[0].grade

    # Expand by quantity
    expanded = []
    for p in pieces:
        for _ in range(p.quantity):
            expanded.append(CutPiece(
                part_mark=p.part_mark, assembly_mark=p.assembly_mark,
                length_inches=p.length_inches, quantity=1,
                shape=p.shape, dimensions=p.dimensions, grade=p.grade,
            ))

    # Sort longest first (FFD/BFD)
    expanded.sort(key=lambda p: p.length_inches, reverse=True)

    bars: List[StockBar] = []

    # Load inventory bars first
    if inventory:
        for inv in inventory:
            for _ in range(inv.get('quantity', 0)):
                bars.append(StockBar(
                    stock_length_ft=inv['length_ft'],
                    shape=shape, dimensions=dimensions, grade=grade,
                    from_inventory=True, inventory_id=inv.get('id', 0),
                ))

    # Best Fit Decreasing
    for piece in expanded:
        # Find best fitting existing bar (smallest remaining that fits)
        best_bar = None
        best_remaining = float('inf')
        for bar in bars:
            if bar.can_fit(piece.length_inches):
                r = bar.remaining - piece.length_inches
                if r < best_remaining:
                    best_bar = bar
                    best_remaining = r

        if best_bar:
            best_bar.add_cut(piece)
        else:
            # Need new bar - pick smallest stock that fits
            piece_ft = piece.length_inches / 12
            chosen = None
            for sl in sorted(stock_lengths_ft):
                if sl * 12 >= piece.length_inches:
                    chosen = sl
                    break
            if not chosen:
                chosen = max(stock_lengths_ft) if stock_lengths_ft else 40

            bar = StockBar(stock_length_ft=chosen, shape=shape, dimensions=dimensions, grade=grade)
            bar.add_cut(piece)
            bars.append(bar)

    # Build purchase list
    purchase = {}
    for bar in bars:
        if not bar.from_inventory:
            key = bar.stock_length_ft
            if key not in purchase:
                purchase[key] = 0
            purchase[key] += 1

    total_stock = sum(b.stock_length_ft for b in bars)
    total_used = sum(b.used_inches / 12 for b in bars)

    return {
        "shape": shape,
        "dimensions": dimensions,
        "grade": grade,
        "bars": [{
            "stock_length_ft": b.stock_length_ft,
            "used_inches": round(b.used_inches, 2),
            "waste_inches": round(b.waste_inches, 2),
            "utilization": b.utilization,
            "from_inventory": b.from_inventory,
            "cuts": [{
                "part_mark": c.part_mark,
                "assembly_mark": c.assembly_mark,
                "length_inches": round(c.length_inches, 2),
                "length_ft": round(c.length_inches / 12, 2),
            } for c in b.cuts],
        } for b in bars],
        "summary": {
            "total_bars": len(bars),
            "from_inventory": sum(1 for b in bars if b.from_inventory),
            "to_purchase": sum(1 for b in bars if not b.from_inventory),
            "total_stock_feet": round(total_stock, 1),
            "total_used_feet": round(total_used, 1),
            "total_waste_feet": round(total_stock - total_used, 1),
            "utilization": round(total_used / total_stock * 100, 1) if total_stock else 0,
            "pieces_nested": len(expanded),
            "purchase_list": [
                {"stock_length_ft": ft, "quantity": qty}
                for ft, qty in sorted(purchase.items())
            ],
        },
    }
