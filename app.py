# ==============================================================================
# IMPORT LIBRARY YANG DIPERLUKAN
# ==============================================================================
import os
import io
import base64
import qrcode
import pandas as pd
import traceback
import logging

from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ==============================================================================
# INISIALISASI APLIKASI FLASK
# ==============================================================================
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = app.logger

# ==============================================================================
# KONFIGURASI KESELAMATAN & DATABASE
# ==============================================================================
app.secret_key = os.environ.get("SECRET_KEY", "g7_aerospace_key_2026")

DB_URL = "postgresql+psycopg2://postgres.yyvrjgdzhliodbgijlgb:KUCINGPUTIH10@aws-1-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=require"

app.config['SQLALCHEMY_DATABASE_URI'] = DB_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

db = SQLAlchemy(app)

# ==============================================================================
# MODEL DATABASE (SKEMA JADUAL)
# ==============================================================================
class RepairLog(db.Model):
    __tablename__ = 'repair_log'

    id            = db.Column(db.Integer,  primary_key=True)
    drn           = db.Column(db.String(100))
    peralatan     = db.Column(db.String(255))
    pn            = db.Column(db.String(255))
    sn            = db.Column(db.String(255))
    date_in       = db.Column(db.Date)
    date_out      = db.Column(db.Date)
    defect        = db.Column(db.Text)
    status_type   = db.Column(db.String(100))
    pic           = db.Column(db.String(255))
    is_warranty   = db.Column(db.Boolean,  default=False)
    created_at    = db.Column(db.DateTime, default=datetime.now)
    last_updated  = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id, 'sn': self.sn,
            'pn': self.pn, 'peralatan': self.peralatan,
            'status': self.status_type
        }

# ==============================================================================
# PERSEDIAAN DATABASE AWAL
# ==============================================================================
with app.app_context():
    try:
        db.create_all()
        print(">>> Sambungan Database Berjaya: Jadual telah disemak/dicipta.")
    except Exception as e:
        print(f">>> Ralat Sambungan Awal Database: {e}")

# ==============================================================================
# FUNGSI BANTUAN (HELPER FUNCTIONS)
# ==============================================================================

def parse_date_input(date_str):
    """Tukar string tarikh HTML form (YYYY-MM-DD) → Python date."""
    if not date_str or date_str.strip() == '':
        return None
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return None


def normalize_status(status_str):
    """
    Normalize status strings → standard categories.
    Handles typos, case, extra spaces, variant spellings.
    OV TDI and OV REPAIR are kept as their own statuses.
    """
    if not status_str:
        return "UNDER REPAIR"

    status = ' '.join(str(status_str).strip().upper().split())  # collapse whitespace

    # ── Exact / prefix matches (most specific first) ──
    if status in ('SERVICEABLE', 'RETURN SERVICEABLE', 'SER'):
        return 'SERVICEABLE'

    if status in ('RETURN UNSERVICEABLE', 'UNSERVICEABLE', 'UNSER'):
        return 'RETURN UNSERVICEABLE'

    if status == 'ISOLATED':
        return 'ISOLATED'

    # ── OV-prefixed statuses  (check BEFORE generic TDI / REPAIR) ──
    if status == 'OV TDI':
        return 'OV TDI'

    if status == 'OV REPAIR':
        return 'OV REPAIR'

    # ── Warranty  ──
    if 'WARRANT' in status or 'WARANT' in status:
        return 'WARRANTY REPAIR'

    # ── TDI sub-statuses  ──
    if 'TDI' in status:
        if 'PROGRESS' in status:
            return 'TDI IN PROGRESS'
        if 'REVIEW' in status:
            return 'TDI TO REVIEW'
        if 'READY' in status and 'QUOTE' in status:
            return 'TDI READY TO QUOTE'
        return 'TDI IN PROGRESS'          # bare "TDI" defaults here

    # ── Quote / Delivery  ──
    if 'READY TO QUOTE' in status or 'READY FOR QUOTE' in status:
        return 'READY TO QUOTE'

    if 'QUOTE SUBMITTED' in status:
        return 'QUOTE SUBMITTED'

    if 'READY TO DELIVERED' in status or 'READY FOR DELIVER' in status:
        return 'READY TO DELIVERED'

    # ── Spare states (SPARE READY before AWAITING SPARE) ──
    if 'SPARE READY' in status:
        return 'SPARE READY'

    if 'AWAITING SPARE' in status or status == 'SPARE':
        return 'AWAITING SPARE'

    # ── Waiting  ──
    if 'WAITING' in status:
        return 'WAITING LO'

    # ── Return  ──
    if 'RETURN' in status and 'AEROTREE' in status:
        return 'RETURN TO AEROTREE'

    # ── Under Repair (catch generic REPAIR last) ──
    if status in ('UNDER REPAIR', 'REPAIR'):
        return 'UNDER REPAIR'

    # No match → return cleaned-up uppercase as-is
    return status


def _parse_import_date(d_str):
    """Shared date parser for bulk-import payloads."""
    if not d_str or str(d_str).strip() in ('', '-', 'None'):
        return None
    try:
        if isinstance(d_str, str):
            return datetime.strptime(d_str.split('T')[0].split(' ')[0], '%Y-%m-%d').date()
        if hasattr(d_str, 'year'):
            return d_str
        return None
    except Exception as e:
        logger.warning(f"Date parse error for '{d_str}': {e}")
        return None


def _build_existing_set():
    """
    Load all existing records from DB for duplicate detection.
    For repair tracking: Only flag as duplicate if P/N + S/N + DATE IN + DATE OUT all match.
    This allows re-repairs of the same part on different dates.
    Returns uppercase keys for case-insensitive comparison.
    """
    rows = db.session.query(RepairLog.pn, RepairLog.sn, RepairLog.date_in, RepairLog.date_out).all()
    return {(
        str(r[0] or '').upper().strip(),  # P/N
        str(r[1] or '').upper().strip(),  # S/N
        str(r[2]) if r[2] else '',        # DATE IN
        str(r[3]) if r[3] else ''         # DATE OUT (added for re-repair support)
    ) for r in rows}


def _process_import_payload(data_list):
    """
    Core import logic shared by /import_bulk and /import_bulk_public.
    • Deduplicates against DB AND within the incoming batch.
    • ✅ PRESERVES EXACT Excel values (no normalization, no uppercase)
    • ✅ Allows re-repairs (only blocks if P/N + S/N + DATE IN + DATE OUT all match)
    • ✅ Items with S/N="N/A" are ALWAYS unique (multiple units without serial numbers)
    Returns (list[RepairLog], skipped_count).
    """
    existing_set  = _build_existing_set()
    seen_in_batch = set()
    logs_to_add   = []
    skipped       = 0

    for item in data_list:
        d_in  = _parse_import_date(item.get('DATE IN'))  or datetime.now().date()
        d_out = _parse_import_date(item.get('DATE OUT'))

        # ✅ NO .upper() - preserve exact case from Excel
        pn_val = str(item.get('P/N',  item.get('PART NO',   'N/A'))).strip()
        sn_val = str(item.get('S/N',  item.get('SERIAL NO', 'N/A'))).strip()
        
        # ✅ SPECIAL RULE: If S/N is "N/A", skip duplicate check (allows multiple units)
        if sn_val.upper() in ('N/A', '', 'NONE', 'NULL'):
            # Don't check for duplicates - always import
            pass
        else:
            # Normal duplicate check for items with real serial numbers
            key = (pn_val.upper(), sn_val.upper(), str(d_in), str(d_out) if d_out else '')
            
            if key in existing_set or key in seen_in_batch:
                skipped += 1
                continue
            seen_in_batch.add(key)

        logs_to_add.append(RepairLog(
            drn        = str(item.get('DRN', '-')).strip(),
            peralatan  = str(item.get('PERALATAN', item.get('DESCRIPTION', 'N/A'))).strip(),
            pn         = pn_val,  # ✅ Exact case from Excel
            sn         = sn_val,  # ✅ Exact case from Excel
            date_in    = d_in,
            date_out   = d_out,
            status_type= str(item.get('STATUS', 'UNDER REPAIR')).strip(),  # ✅ Exact from Excel
            pic        = str(item.get('PIC', 'N/A')).strip(),              # ✅ Exact from Excel
            defect     = str(item.get('DEFECT', 'N/A')).strip()            # ✅ Exact from Excel
        ))

    return logs_to_add, skipped


# ==============================================================================
# LALUAN (ROUTES) - HALAMAN UTAMA & LOGIN
# ==============================================================================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    next_page = request.args.get('next')

    if request.method == 'POST':
        username = request.form.get('u')
        password = request.form.get('p')

        if username == 'admin' and password == 'password123':
            session['admin'] = True
            flash("Log masuk berjaya!", "success")
            target = request.form.get('next_target')
            if target and target != 'None' and target != '':
                return redirect(target)
            return redirect(url_for('admin'))
        else:
            flash("Username atau Password salah!", "error")

    return render_template('login.html', next_page=next_page)


@app.route('/logout')
def logout():
    session.clear()
    flash("Anda telah log keluar.", "info")
    return redirect(url_for('index'))


@app.route('/health')
def health():
    """
    Health check endpoint for monitoring/cronjobs.
    Returns 200 OK if service is running.
    No authentication required.
    """
    return jsonify({
        "status": "ok",
        "service": "G7 Aerospace MRO System",
        "timestamp": datetime.now().isoformat()
    }), 200


# ==============================================================================
# LALUAN (ROUTES) - DASHBOARD ADMIN
# ==============================================================================

@app.route('/admin')
def admin():
    if not session.get('admin'):
        return redirect(url_for('login', next=request.path))

    try:
        logs = RepairLog.query.order_by(RepairLog.id.desc()).all()

        # ── Canonical status list (stats table rows) ──
        status_list = [
            "SERVICEABLE",
            "RETURN UNSERVICEABLE",
            "UNDER REPAIR",
            "OV REPAIR",
            "OV TDI",
            "WARRANTY REPAIR",
            "TDI IN PROGRESS",
            "TDI TO REVIEW",
            "TDI READY TO QUOTE",
            "READY TO QUOTE",
            "QUOTE SUBMITTED",
            "READY TO DELIVERED",
            "READY TO DELIVERED WARRANTY",
            "WAITING LO",
            "AWAITING SPARE",
            "SPARE READY",
            "ISOLATED",
            "RETURN TO AEROTREE",
        ]

        # Add any DB status not yet in the list (future-proof)
        for (s,) in db.session.query(RepairLog.status_type).distinct():
            if s:
                up = s.upper().strip()
                if up not in status_list:
                    status_list.append(up)

        # ── Year columns ──
        years = sorted({l.date_in.year for l in logs if l.date_in}) or [datetime.now().year]

        # ── Stats matrix ──
        stats_matrix   = {st: {y: 0 for y in years} for st in status_list}
        row_totals     = {st: 0 for st in status_list}
        column_totals  = {y: 0 for y in years}
        grand_total    = 0

        for l in logs:
            if l.date_in and l.status_type:
                sk = l.status_type.upper().strip()
                yk = l.date_in.year
                if sk in stats_matrix and yk in years:
                    stats_matrix[sk][yk] += 1
                    row_totals[sk]       += 1
                    column_totals[yk]    += 1
                    grand_total          += 1

        return render_template('admin.html',
                               logs=logs,
                               sorted_years=years,
                               years=years,
                               status_list=status_list,
                               stats_matrix=stats_matrix,
                               row_totals=row_totals,
                               column_totals=column_totals,
                               grand_total=grand_total,
                               total_units=len(logs),
                               stats=column_totals)

    except Exception as e:
        logger.error(f"Admin Dashboard Error: {e}")
        return f"<h3>Admin Dashboard Error (500)</h3><p>{e}</p><pre>{traceback.format_exc()}</pre>", 500


# ==============================================================================
# LALUAN (ROUTES) - DATA MASUK & IMPORT
# ==============================================================================

@app.route('/incoming', methods=['GET', 'POST'])
def incoming():
    if request.method == 'GET':
        return render_template('incoming.html')

    try:
        status_val = request.form.get('status') or request.form.get('status_type') or "UNDER REPAIR"

        d_in = parse_date_input(request.form.get('date_in')) or datetime.now().date()

        new_log = RepairLog(
            drn        = request.form.get('drn', '').upper(),
            peralatan  = request.form.get('peralatan', '').upper(),
            pn         = request.form.get('pn', '').upper(),
            sn         = request.form.get('sn', '').upper(),
            date_in    = d_in,
            defect     = request.form.get('defect', 'N/A').upper(),
            status_type= normalize_status(status_val),          # ✅ normalized
            pic        = request.form.get('pic', 'N/A').upper()
        )

        db.session.add(new_log)
        db.session.commit()

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify({"status": "success", "message": "Data Berjaya Disimpan!"}), 200

        flash("Data Berjaya Disimpan!", "success")
        return redirect(url_for('index'))

    except Exception as e:
        db.session.rollback()
        logger.error(f"Incoming Data Error: {e}")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"status": "error", "message": str(e)}), 500
        return f"Database Error: {str(e)}", 500


@app.route('/import_bulk', methods=['POST'])
def import_bulk():
    """Admin bulk import (login required). Duplicate-safe + normalized."""
    if not session.get('admin'):
        return jsonify({"error": "Unauthorized"}), 403

    data_list = request.json.get('data', [])
    if not data_list:
        return jsonify({"error": "No data received"}), 400

    try:
        logs_to_add, skipped = _process_import_payload(data_list)
        if logs_to_add:
            db.session.bulk_save_objects(logs_to_add)
            db.session.commit()
        return jsonify({"status": "success", "count": len(logs_to_add), "skipped": skipped}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Bulk Import Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/import_bulk_public', methods=['POST'])
def import_bulk_public():
    """Public bulk import (no login). Used by index.html. Duplicate-safe + normalized."""
    data_list = request.json.get('data', [])
    if not data_list:
        return jsonify({"error": "No data received"}), 400

    try:
        logs_to_add, skipped = _process_import_payload(data_list)
        if logs_to_add:
            db.session.bulk_save_objects(logs_to_add)
            db.session.commit()
        return jsonify({"status": "success", "count": len(logs_to_add), "skipped": skipped}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Public Bulk Import Error: {e}")
        return jsonify({"error": str(e)}), 500


# ==============================================================================
# LALUAN (ROUTES) - EDIT, ISOLATE & DELETE
# ==============================================================================

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
def edit(id):
    if not session.get('admin'):
        return redirect(url_for('login', next=request.full_path))

    l      = RepairLog.query.get_or_404(id)
    source = request.args.get('from', request.form.get('origin_source', 'admin'))

    if request.method == 'POST':
        try:
            l.peralatan = request.form.get('peralatan', '').upper()
            l.pn        = request.form.get('pn', '').upper()
            l.sn        = request.form.get('sn', '').upper()
            l.drn       = request.form.get('drn', '').upper()
            l.pic       = request.form.get('pic', '').upper()
            l.defect    = request.form.get('defect', '').upper()

            new_status = request.form.get('status') or request.form.get('status_type')
            if new_status:
                l.status_type = normalize_status(new_status)  # ✅ normalized

            d_in_str = request.form.get('date_in')
            if d_in_str:
                l.date_in = datetime.strptime(d_in_str, '%Y-%m-%d').date()

            d_out_str = request.form.get('date_out')
            l.date_out = datetime.strptime(d_out_str, '%Y-%m-%d').date() if d_out_str and d_out_str.strip() else None

            l.last_updated = datetime.now()
            db.session.commit()
            flash("Rekod Berjaya Dikemaskini!", "success")

            return redirect(url_for('view_tag', id=id) if source == 'view_tag' else url_for('admin'))

        except Exception as e:
            db.session.rollback()
            logger.error(f"Edit Error ID {id}: {e}")
            flash(f"Ralat Simpan: {str(e)}", "error")

    return render_template('edit.html', item=l, source=source)


@app.route('/isolate/<int:id>')
def isolate_log(id):
    """Set status → ISOLATED, clear date_out. Admin only."""
    if not session.get('admin'):
        return redirect(url_for('login', next=request.path))
    try:
        l = RepairLog.query.get_or_404(id)
        l.status_type  = 'ISOLATED'
        l.date_out     = None
        l.last_updated = datetime.now()
        db.session.commit()
        flash("Rekod telah di-isolate.", "success")
    except Exception as e:
        db.session.rollback()
        flash("Gagal isolate rekod.", "error")
    return redirect(url_for('admin'))


@app.route('/delete/<int:id>')
def delete_log(id):
    """Padam satu rekod."""
    if not session.get('admin'):
        return redirect(url_for('login', next=request.path))
    try:
        db.session.delete(RepairLog.query.get_or_404(id))
        db.session.commit()
        flash("Rekod dipadam.", "success")
    except Exception as e:
        db.session.rollback()
        flash("Gagal memadam rekod.", "error")
    return redirect(url_for('admin'))


@app.route('/delete_bulk', methods=['POST'])
def bulk_delete():
    """Padam multiple records dari checkbox."""
    if not session.get('admin'):
        return redirect(url_for('login', next=request.path))

    selected_ids = request.form.getlist('ids')
    if selected_ids:
        try:
            ids_int = [int(i) for i in selected_ids]
            RepairLog.query.filter(RepairLog.id.in_(ids_int)).delete(synchronize_session=False)
            db.session.commit()
            flash(f"{len(ids_int)} rekod berjaya dipadam secara pukal.", "success")
        except Exception as e:
            db.session.rollback()
            logger.error(f"Bulk Delete Error: {e}")
            flash("Ralat semasa memadam rekod.", "error")
    else:
        flash("Tiada rekod dipilih untuk dipadam.", "warning")
    return redirect(url_for('admin'))


# ==============================================================================
# LALUAN (ROUTES) - VIEW, HISTORY & REPORT
# ==============================================================================

@app.route('/history/<path:sn>')
def history(sn):
    logs = RepairLog.query.filter_by(sn=sn).order_by(RepairLog.date_in.desc()).all()
    asset_info = logs[0] if logs else {"peralatan": "UNKNOWN", "sn": sn}
    return render_template('history.html', logs=logs, asset=asset_info, sn=sn)


@app.route('/view_report/<int:id>')
def view_report(id):
    if not session.get('admin'):
        return redirect(url_for('login', next=request.path))
    return render_template('view_report.html', l=RepairLog.query.get_or_404(id))


@app.route('/view_tag/<int:id>')
def view_tag(id):
    l     = RepairLog.query.get_or_404(id)
    count = RepairLog.query.filter_by(sn=l.sn).count()
    return render_template('view_tag.html', l=l, logs_count=count)


# ==============================================================================
# LALUAN (ROUTES) - JANA FILE (PDF / EXCEL / QR)
# ==============================================================================

@app.route('/download_qr/<int:id>')
def download_qr(id):
    l      = RepairLog.query.get_or_404(id)
    qr_url = f"{request.url_root}view_tag/{l.id}"
    qr     = qrcode.make(qr_url)
    buf    = io.BytesIO()
    qr.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype='image/png', as_attachment=True, download_name=f"QR_{l.sn}.png")


@app.route('/download_report')
def download_report():
    if not session.get('admin'):
        return redirect(url_for('login', next=request.path))
    try:
        logs = RepairLog.query.order_by(RepairLog.id.desc()).all()
        buf  = io.BytesIO()
        doc  = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=15, rightMargin=15)

        styles = getSampleStyleSheet()
        cell   = ParagraphStyle(name='TableCell', fontSize=7, leading=8, alignment=1)

        elements = [
            Paragraph(f"G7 AEROSPACE - REPAIR LOG SUMMARY ({datetime.now().strftime('%d/%m/%Y')})", styles['Title']),
            Spacer(1, 12),
        ]

        data = [["ID", "PERALATAN", "P/N", "S/N", "DEFECT", "DATE IN", "DATE OUT", "STATUS", "PIC"]]
        for l in logs:
            data.append([
                l.id,
                Paragraph(l.peralatan or "N/A", cell),
                Paragraph(l.pn or "N/A", cell),
                l.sn,
                Paragraph(l.defect or "N/A", cell),
                str(l.date_in),
                str(l.date_out) if l.date_out else "-",
                l.status_type,
                Paragraph(l.pic or "N/A", cell),
            ])

        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND',    (0,0), (-1,0),  colors.HexColor('#1e293b')),
            ('TEXTCOLOR',     (0,0), (-1,0),  colors.whitesmoke),
            ('GRID',          (0,0), (-1,-1), 0.5, colors.black),
            ('ROWBACKGROUNDS',(0,1), (-1,-1), [colors.white, colors.HexColor('#f1f5f9')]),
            ('FONTSIZE',      (0,0), (-1,-1), 7),
            ('ALIGN',         (0,0), (-1,-1), 'CENTER'),
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
        ]))
        elements.append(t)
        doc.build(elements)
        buf.seek(0)
        return send_file(buf, mimetype='application/pdf', as_attachment=True, download_name="Full_Summary.pdf")
    except Exception as e:
        logger.error(f"PDF Generation Error: {e}")
        return f"Error Generating PDF: {e}"


@app.route('/export_excel')
def export_excel_data():
    if not session.get('admin'):
        return redirect(url_for('login', next=request.path))
    try:
        from openpyxl import Workbook
        from openpyxl.styles import (Font, PatternFill, Alignment, Border, Side,
                                     GradientFill)
        from openpyxl.utils import get_column_letter
        from openpyxl.styles.numbers import FORMAT_DATE_DDMMYY

        logs = RepairLog.query.order_by(RepairLog.date_in.asc(), RepairLog.id.asc()).all()

        # ── Colour palette ──────────────────────────────────────────────────────
        NAVY        = "0F172A"
        PURPLE      = "7C3AED"
        CYAN        = "0891B2"
        GREEN       = "059669"
        ORANGE      = "EA580C"
        RED         = "BE123C"
        AMBER       = "D97706"
        BLUE        = "1D4ED8"
        WHITE       = "FFFFFF"
        LIGHT_GRAY  = "F1F5F9"
        DARK_GRAY   = "334155"

        # year → accent colour
        YEAR_COLORS = {
            2022: "F97316",   # orange
            2023: "8B5CF6",   # purple
            2024: "06B6D4",   # cyan
            2025: "10B981",   # green
            2026: "F43F5E",   # pink-red
        }

        # status → fill colour
        STATUS_COLORS = {
            "SERVICEABLE":            "D1FAE5",
            "RETURN UNSERVICEABLE":   "FEE2E2",
            "UNDER REPAIR":           "DBEAFE",
            "OV REPAIR":              "DBEAFE",
            "OV TDI":                 "FEF3C7",
            "WARRANTY REPAIR":        "FFEDD5",
            "TDI IN PROGRESS":        "FEF3C7",
            "TDI TO REVIEW":          "FEF3C7",
            "TDI READY TO QUOTE":     "FEF3C7",
            "READY TO QUOTE":         "E0F2FE",
            "QUOTE SUBMITTED":        "E0F2FE",
            "READY TO DELIVERED":     "D1FAE5",
            "AWAITING SPARE":         "EDE9FE",
            "SPARE READY":            "D1FAE5",
            "WAITING LO":             "FEF9C3",
            "ISOLATED":               "F1F5F9",
            "RETURN TO AEROTREE":     "FEE2E2",
        }
        STATUS_TEXT = {
            "SERVICEABLE":            "065F46",
            "RETURN UNSERVICEABLE":   "991B1B",
            "UNDER REPAIR":           "1E40AF",
            "OV REPAIR":              "1E40AF",
            "OV TDI":                 "92400E",
            "WARRANTY REPAIR":        "9A3412",
            "TDI IN PROGRESS":        "92400E",
            "TDI TO REVIEW":          "92400E",
            "TDI READY TO QUOTE":     "92400E",
            "READY TO QUOTE":         "075985",
            "QUOTE SUBMITTED":        "075985",
            "READY TO DELIVERED":     "065F46",
            "AWAITING SPARE":         "4C1D95",
            "SPARE READY":            "065F46",
            "WAITING LO":             "713F12",
            "ISOLATED":               "475569",
            "RETURN TO AEROTREE":     "991B1B",
        }

        def solid(hex_color):
            return PatternFill("solid", fgColor=hex_color)

        def thin_border(color="CBD5E1"):
            s = Side(style="thin", color=color)
            return Border(left=s, right=s, top=s, bottom=s)

        def center_align(wrap=False):
            return Alignment(horizontal="center", vertical="center", wrap_text=wrap)

        def left_align(wrap=False):
            return Alignment(horizontal="left", vertical="center", wrap_text=wrap)

        wb = Workbook()

        # ── Group logs by year ──────────────────────────────────────────────────
        from collections import defaultdict
        by_year = defaultdict(list)
        for l in logs:
            yr = l.date_in.year if l.date_in else 0
            by_year[yr].append(l)

        sorted_years = sorted(by_year.keys())

        # ══════════════════════════════════════════════════════════════════════════
        #  SHEET 1 – SUMMARY DASHBOARD
        # ══════════════════════════════════════════════════════════════════════════
        ws_sum = wb.active
        ws_sum.title = "📊 Summary"
        ws_sum.sheet_view.showGridLines = False

        # Title block
        ws_sum.merge_cells("A1:H1")
        t = ws_sum["A1"]
        t.value = "G7 AEROSPACE — MAINTENANCE RECORD EXPORT"
        t.font  = Font(name="Arial", bold=True, size=18, color=WHITE)
        t.fill  = solid(NAVY)
        t.alignment = center_align()
        ws_sum.row_dimensions[1].height = 44

        ws_sum.merge_cells("A2:H2")
        sub = ws_sum["A2"]
        sub.value = f"Generated: {datetime.now().strftime('%d %B %Y  %H:%M')}   |   Total Records: {len(logs)}"
        sub.font  = Font(name="Arial", size=10, color="94A3B8")
        sub.fill  = solid(NAVY)
        sub.alignment = center_align()
        ws_sum.row_dimensions[2].height = 22

        ws_sum.row_dimensions[3].height = 10   # spacer

        # Status summary header
        headers_sum = ["STATUS", "TOTAL"] + [str(y) for y in sorted_years]
        col_colors  = [NAVY, DARK_GRAY] + [YEAR_COLORS.get(y, DARK_GRAY) for y in sorted_years]

        for ci, (h, cc) in enumerate(zip(headers_sum, col_colors), start=1):
            cell = ws_sum.cell(row=4, column=ci, value=h)
            cell.font      = Font(name="Arial", bold=True, size=9, color=WHITE)
            cell.fill      = solid(cc)
            cell.alignment = center_align()
            cell.border    = thin_border(cc)
        ws_sum.row_dimensions[4].height = 28

        # Build status matrix
        status_list = [
            "SERVICEABLE","RETURN UNSERVICEABLE","UNDER REPAIR","OV REPAIR","OV TDI",
            "WARRANTY REPAIR","TDI IN PROGRESS","TDI TO REVIEW","TDI READY TO QUOTE",
            "READY TO QUOTE","QUOTE SUBMITTED","READY TO DELIVERED","AWAITING SPARE",
            "SPARE READY","WAITING LO","ISOLATED","RETURN TO AEROTREE",
        ]
        # add any unexpected statuses
        for l in logs:
            if l.status_type and l.status_type.upper() not in status_list:
                status_list.append(l.status_type.upper())

        matrix = {st: {y: 0 for y in sorted_years} for st in status_list}
        year_totals = {y: 0 for y in sorted_years}
        for l in logs:
            yr = l.date_in.year if l.date_in else None
            st = (l.status_type or "").upper().strip()
            if yr and st in matrix:
                matrix[st][yr] += 1
                year_totals[yr] += 1

        for ri, st in enumerate(status_list, start=5):
            row_total = sum(matrix[st].values())
            bg  = STATUS_COLORS.get(st, "F8FAFC")
            txt = STATUS_TEXT.get(st, "1E293B")

            # status name
            c0 = ws_sum.cell(row=ri, column=1, value=st)
            c0.font      = Font(name="Arial", bold=True, size=9, color=txt)
            c0.fill      = solid(bg)
            c0.alignment = left_align()
            c0.border    = thin_border()

            # total
            c1 = ws_sum.cell(row=ri, column=2, value=row_total)
            c1.font      = Font(name="Arial", bold=True, size=10,
                                color=WHITE if row_total > 0 else "94A3B8")
            c1.fill      = solid(PURPLE if row_total > 0 else "F1F5F9")
            c1.alignment = center_align()
            c1.border    = thin_border()

            # per-year cells
            for ci, yr in enumerate(sorted_years, start=3):
                v   = matrix[st][yr]
                yc  = YEAR_COLORS.get(yr, DARK_GRAY)
                cell = ws_sum.cell(row=ri, column=ci, value=v if v > 0 else "—")
                cell.font      = Font(name="Arial", bold=(v > 0), size=10,
                                      color=yc if v > 0 else "CBD5E1")
                cell.fill      = solid("F8FAFC")
                cell.alignment = center_align()
                cell.border    = thin_border()
            ws_sum.row_dimensions[ri].height = 22

        # Grand total row
        gt_row = len(status_list) + 5
        ws_sum.cell(row=gt_row, column=1, value="GRAND TOTAL").font = Font(
            name="Arial", bold=True, size=10, color=WHITE)
        ws_sum.cell(row=gt_row, column=1).fill      = solid(NAVY)
        ws_sum.cell(row=gt_row, column=1).alignment = left_align()
        ws_sum.cell(row=gt_row, column=1).border    = thin_border(NAVY)

        ws_sum.cell(row=gt_row, column=2, value=len(logs)).font = Font(
            name="Arial", bold=True, size=12, color=WHITE)
        ws_sum.cell(row=gt_row, column=2).fill      = solid(PURPLE)
        ws_sum.cell(row=gt_row, column=2).alignment = center_align()
        ws_sum.cell(row=gt_row, column=2).border    = thin_border(PURPLE)

        for ci, yr in enumerate(sorted_years, start=3):
            v    = year_totals[yr]
            yc   = YEAR_COLORS.get(yr, DARK_GRAY)
            cell = ws_sum.cell(row=gt_row, column=ci, value=v)
            cell.font      = Font(name="Arial", bold=True, size=11, color=WHITE)
            cell.fill      = solid(yc)
            cell.alignment = center_align()
            cell.border    = thin_border(yc)
        ws_sum.row_dimensions[gt_row].height = 30

        # Column widths for summary
        ws_sum.column_dimensions["A"].width = 32
        ws_sum.column_dimensions["B"].width = 12
        for ci in range(3, 3 + len(sorted_years)):
            ws_sum.column_dimensions[get_column_letter(ci)].width = 12

        # ══════════════════════════════════════════════════════════════════════════
        #  SHEETS 2..N – ONE PER YEAR
        # ══════════════════════════════════════════════════════════════════════════
        COL_HEADERS = ["#", "EQUIPMENT", "PART NUMBER", "SERIAL NUMBER",
                       "DEFECT / REMARKS", "DATE IN", "DATE OUT", "STATUS", "PIC / JTP"]
        COL_WIDTHS  = [6, 32, 20, 20, 40, 13, 13, 26, 22]

        for yr in sorted_years:
            year_logs = by_year[yr]
            yc = YEAR_COLORS.get(yr, DARK_GRAY)

            ws = wb.create_sheet(title=f"Year {yr}")
            ws.sheet_view.showGridLines = False

            # ── Title bar ──
            last_col = get_column_letter(len(COL_HEADERS))
            ws.merge_cells(f"A1:{last_col}1")
            tc = ws["A1"]
            tc.value     = f"G7 AEROSPACE — MAINTENANCE RECORDS  ·  {yr}"
            tc.font      = Font(name="Arial", bold=True, size=16, color=WHITE)
            tc.fill      = solid(NAVY)
            tc.alignment = center_align()
            ws.row_dimensions[1].height = 42

            ws.merge_cells(f"A2:{last_col}2")
            sc = ws["A2"]
            sc.value     = (f"Total units for {yr}: {len(year_logs)}  |  "
                            f"Generated: {datetime.now().strftime('%d %B %Y')}")
            sc.font      = Font(name="Arial", size=9, italic=True, color="CBD5E1")
            sc.fill      = solid(yc)
            sc.alignment = center_align()
            ws.row_dimensions[2].height = 20

            ws.row_dimensions[3].height = 8  # spacer

            # ── Column headers ──
            for ci, (hdr, w) in enumerate(zip(COL_HEADERS, COL_WIDTHS), start=1):
                cell = ws.cell(row=4, column=ci, value=hdr)
                cell.font      = Font(name="Arial", bold=True, size=9, color=WHITE)
                cell.fill      = solid(yc)
                cell.alignment = center_align(wrap=True)
                cell.border    = thin_border(yc)
                ws.column_dimensions[get_column_letter(ci)].width = w
            ws.row_dimensions[4].height = 30
            ws.freeze_panes = "A5"

            # ── Data rows ──
            for ri, l in enumerate(year_logs, start=5):
                st     = (l.status_type or "").upper().strip()
                bg     = STATUS_COLORS.get(st, "F8FAFC")
                txt    = STATUS_TEXT.get(st, "1E293B")
                row_bg = "FFFFFF" if ri % 2 == 0 else LIGHT_GRAY

                row_data = [
                    ri - 4,
                    l.peralatan or "N/A",
                    l.pn or "N/A",
                    l.sn or "N/A",
                    l.defect or "N/A",
                    str(l.date_in) if l.date_in else "—",
                    str(l.date_out) if l.date_out else "—",
                    st,
                    l.pic or "N/A",
                ]

                for ci, val in enumerate(row_data, start=1):
                    cell = ws.cell(row=ri, column=ci, value=val)
                    cell.border = thin_border("E2E8F0")

                    if ci == 1:   # row number
                        cell.font      = Font(name="Arial", size=8, color="94A3B8")
                        cell.fill      = solid(row_bg)
                        cell.alignment = center_align()
                    elif ci == 2: # equipment – bold
                        cell.font      = Font(name="Arial", bold=True, size=10, color="0F172A")
                        cell.fill      = solid(row_bg)
                        cell.alignment = left_align(wrap=True)
                    elif ci in (3, 4): # P/N, S/N – monospace style
                        cell.font      = Font(name="Courier New", size=9,
                                              color="1E40AF" if ci == 4 else "334155")
                        cell.fill      = solid("EFF6FF" if ci == 4 else row_bg)
                        cell.alignment = center_align()
                    elif ci == 5: # defect – italic amber
                        cell.font      = Font(name="Arial", size=9, italic=True, color="92400E")
                        cell.fill      = solid("FFFBEB")
                        cell.alignment = left_align(wrap=True)
                    elif ci in (6, 7): # dates
                        cell.font      = Font(name="Arial", size=9, color="475569")
                        cell.fill      = solid(row_bg)
                        cell.alignment = center_align()
                    elif ci == 8: # status badge
                        cell.font      = Font(name="Arial", bold=True, size=8, color=txt)
                        cell.fill      = solid(bg)
                        cell.alignment = center_align(wrap=True)
                    elif ci == 9: # PIC
                        cell.font      = Font(name="Arial", size=9, color="475569")
                        cell.fill      = solid(row_bg)
                        cell.alignment = left_align(wrap=True)

                ws.row_dimensions[ri].height = 20

        # ── Remove default empty sheet if still present ──
        if "Sheet" in wb.sheetnames:
            del wb["Sheet"]

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        fname = f"G7_Aerospace_{datetime.now().strftime('%Y%m%d')}.xlsx"
        return send_file(output,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=fname)

    except Exception as e:
        logger.error(f"Excel Export Error: {e}")
        import traceback
        return f"Error Exporting Excel: {e}<br><pre>{traceback.format_exc()}</pre>"


# ==============================================================================
# LALUAN (ROUTES) - DB MAINTENANCE
# ==============================================================================

@app.route('/cleanup_duplicates', methods=['POST'])
def cleanup_duplicates():
    """
    Remove exact duplicates (same P/N + S/N + DATE IN + DATE OUT).
    Keeps lowest-ID record in each group. Admin only.
    ✅ Allows re-repairs (only removes if all 4 fields match)
    """
    if not session.get('admin'):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        from sqlalchemy import func

        dup_groups = (
            db.session.query(
                RepairLog.pn, RepairLog.sn, RepairLog.date_in, RepairLog.date_out,
                func.min(RepairLog.id).label('keep_id')
            )
            .group_by(RepairLog.pn, RepairLog.sn, RepairLog.date_in, RepairLog.date_out)
            .having(func.count(RepairLog.id) > 1)
            .all()
        )

        deleted = 0
        for g in dup_groups:
            deleted += (
                RepairLog.query
                .filter(
                    RepairLog.pn      == g.pn,
                    RepairLog.sn      == g.sn,
                    RepairLog.date_in == g.date_in,
                    RepairLog.date_out == g.date_out,
                    RepairLog.id      != g.keep_id
                )
                .delete()
            )
        db.session.commit()
        logger.info(f"Cleanup: deleted {deleted} duplicate records")
        return jsonify({"status": "success", "deleted": deleted}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Cleanup Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/clear_all', methods=['POST'])
def clear_all():
    """Padam SEMUA rekod. Admin only. Guna sebelum fresh reimport."""
    if not session.get('admin'):
        return jsonify({"error": "Unauthorized"}), 403
    try:
        RepairLog.query.delete()
        db.session.commit()
        logger.info("All records cleared by admin")
        return jsonify({"status": "success", "message": "All records deleted"}), 200
    except Exception as e:
        db.session.rollback()
        logger.error(f"Clear All Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/normalize_existing_statuses')
def normalize_existing_statuses():
    """
    ONE-TIME maintenance route.
    Visit /normalize_existing_statuses while logged-in as admin to
    rewrite every status_type through normalize_status().
    Idempotent — safe to run multiple times.
    """
    if not session.get('admin'):
        return redirect(url_for('login'))
    try:
        all_logs      = RepairLog.query.all()
        updated_count = 0
        changes       = {}

        for log in all_logs:
            old = log.status_type
            new = normalize_status(old)
            if old != new:
                key = f"{old} → {new}"
                changes[key] = changes.get(key, 0) + 1
                log.status_type  = new
                log.last_updated = datetime.now()
                updated_count   += 1

        db.session.commit()

        report = f"✅ NORMALIZATION COMPLETE — {updated_count} record(s) updated.\n"
        for change, cnt in sorted(changes.items(), key=lambda x: -x[1]):
            report += f"  • {change}  ({cnt})\n"
        flash(report, "success")
    except Exception as e:
        db.session.rollback()
        flash(f"❌ Error: {str(e)}", "error")
    return redirect(url_for('admin'))


# ==============================================================================
# ENTRY POINT
# ==============================================================================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)