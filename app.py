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
        from collections import Counter

        logs   = RepairLog.query.order_by(RepairLog.id.desc()).all()
        output = io.BytesIO()

        STATUS_COLORS = {
            "SERVICEABLE":                 {"bg": "16A34A", "fg": "FFFFFF"},
            "RETURN UNSERVICEABLE":        {"bg": "DC2626", "fg": "FFFFFF"},
            "UNDER REPAIR":               {"bg": "D97706", "fg": "FFFFFF"},
            "OV REPAIR":                  {"bg": "9333EA", "fg": "FFFFFF"},
            "OV TDI":                     {"bg": "7C3AED", "fg": "FFFFFF"},
            "WARRANTY REPAIR":            {"bg": "0369A1", "fg": "FFFFFF"},
            "TDI IN PROGRESS":            {"bg": "0891B2", "fg": "FFFFFF"},
            "TDI TO REVIEW":              {"bg": "06B6D4", "fg": "1E293B"},
            "TDI READY TO QUOTE":         {"bg": "67E8F9", "fg": "1E293B"},
            "READY TO QUOTE":             {"bg": "FBBF24", "fg": "1E293B"},
            "QUOTE SUBMITTED":            {"bg": "F59E0B", "fg": "FFFFFF"},
            "READY TO DELIVERED":         {"bg": "10B981", "fg": "FFFFFF"},
            "READY TO DELIVERED WARRANTY":{"bg": "059669", "fg": "FFFFFF"},
            "WAITING LO":                 {"bg": "94A3B8", "fg": "FFFFFF"},
            "AWAITING SPARE":             {"bg": "F97316", "fg": "FFFFFF"},
            "SPARE READY":                {"bg": "84CC16", "fg": "1E293B"},
            "ISOLATED":                   {"bg": "64748B", "fg": "FFFFFF"},
            "RETURN TO AEROTREE":         {"bg": "BE185D", "fg": "FFFFFF"},
        }

        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            wb = writer.book
            def _fmt(p): return wb.add_format(p)

            fmt_title   = _fmt({'bold':True,'font_size':18,'font_color':'#FFFFFF','bg_color':'#0F172A','align':'left','valign':'vcenter','font_name':'Arial'})
            fmt_sub     = _fmt({'font_size':9,'font_color':'#94A3B8','bg_color':'#0F172A','align':'left','valign':'vcenter','font_name':'Arial'})
            fmt_blank   = _fmt({'bg_color':'#0F172A'})
            fmt_hdr     = _fmt({'bold':True,'font_size':9,'font_color':'#FFFFFF','bg_color':'#1E293B','align':'center','valign':'vcenter','border':1,'border_color':'#334155','text_wrap':True,'font_name':'Arial'})
            fmt_id      = _fmt({'bold':True,'font_size':9,'align':'center','valign':'vcenter','bg_color':'#1E293B','font_color':'#94A3B8','border':1,'border_color':'#334155','font_name':'Arial'})
            fmt_odd     = _fmt({'font_size':9,'align':'left','valign':'vcenter','bg_color':'#F8FAFC','border':1,'border_color':'#E2E8F0','font_name':'Arial','text_wrap':True})
            fmt_even    = _fmt({'font_size':9,'align':'left','valign':'vcenter','bg_color':'#FFFFFF','border':1,'border_color':'#E2E8F0','font_name':'Arial','text_wrap':True})
            fmt_pn_odd  = _fmt({'font_size':9,'bold':True,'align':'center','valign':'vcenter','bg_color':'#F8FAFC','font_color':'#1D4ED8','border':1,'border_color':'#E2E8F0','font_name':'Courier New'})
            fmt_pn_even = _fmt({'font_size':9,'bold':True,'align':'center','valign':'vcenter','bg_color':'#FFFFFF','font_color':'#1D4ED8','border':1,'border_color':'#E2E8F0','font_name':'Courier New'})
            fmt_dt_odd  = _fmt({'font_size':9,'align':'center','valign':'vcenter','bg_color':'#F8FAFC','border':1,'border_color':'#E2E8F0','font_name':'Arial Narrow'})
            fmt_dt_even = _fmt({'font_size':9,'align':'center','valign':'vcenter','bg_color':'#FFFFFF','border':1,'border_color':'#E2E8F0','font_name':'Arial Narrow'})
            fmt_dash    = _fmt({'font_size':9,'align':'center','valign':'vcenter','bg_color':'#F1F5F9','font_color':'#CBD5E1','border':1,'border_color':'#E2E8F0','font_name':'Arial'})
            fmt_def_odd = _fmt({'font_size':8,'italic':True,'align':'left','valign':'vcenter','bg_color':'#FFF7ED','font_color':'#9A3412','border':1,'border_color':'#FDBA74','font_name':'Arial','text_wrap':True})
            fmt_def_even= _fmt({'font_size':8,'italic':True,'align':'left','valign':'vcenter','bg_color':'#FFF7ED','font_color':'#9A3412','border':1,'border_color':'#FDBA74','font_name':'Arial','text_wrap':True})
            fmt_tot_lbl = _fmt({'bold':True,'font_size':10,'font_color':'#FFFFFF','bg_color':'#0F172A','align':'right','valign':'vcenter','border':1,'border_color':'#334155','font_name':'Arial'})
            fmt_tot_val = _fmt({'bold':True,'font_size':10,'font_color':'#FBBF24','bg_color':'#0F172A','align':'center','valign':'vcenter','border':1,'border_color':'#334155','font_name':'Arial'})

            # ── Sheet 1: Repair Log ──────────────────────────────────
            ws = wb.add_worksheet('Repair Log')
            writer.sheets['Repair Log'] = ws
            ws.set_zoom(90)
            ws.freeze_panes(4, 0)
            ws.set_row(0, 34); ws.set_row(1, 18); ws.set_row(2, 5); ws.set_row(3, 28)

            COLS  = ["ID","DRN","EQUIPMENT","P/N","S/N","DEFECT / REMARKS","DATE IN","DATE OUT","STATUS","PIC / JTP"]
            COL_W = [5, 12, 30, 18, 18, 42, 13, 13, 24, 22]
            for i, w in enumerate(COL_W): ws.set_column(i, i, w)

            ws.merge_range('A1:J1', '  G7 AEROSPACE  -  MAINTENANCE REPAIR LOG', fmt_title)
            ws.merge_range('A2:J2', f'  Generated: {datetime.now().strftime("%d %B %Y  |  %H:%M")}   |   Total Records: {len(logs)}', fmt_sub)
            ws.merge_range('A3:J3', '', fmt_blank)
            for ci, col in enumerate(COLS): ws.write(3, ci, col, fmt_hdr)

            for ri, l in enumerate(logs):
                row = ri + 4
                ws.set_row(row, 20)
                odd = ri % 2 == 0
                ws.write(row, 0, l.id,                         fmt_id)
                ws.write(row, 1, l.drn or '-',                 fmt_odd  if odd else fmt_even)
                ws.write(row, 2, l.peralatan or '-',           fmt_odd  if odd else fmt_even)
                ws.write(row, 3, l.pn or '-',                  fmt_pn_odd if odd else fmt_pn_even)
                ws.write(row, 4, l.sn or '-',                  fmt_pn_odd if odd else fmt_pn_even)
                ws.write(row, 5, l.defect or 'N/A',            fmt_def_odd if odd else fmt_def_even)
                ws.write(row, 6, str(l.date_in) if l.date_in else '-',
                                                               fmt_dt_odd if odd else fmt_dt_even)
                dout = str(l.date_out) if l.date_out else None
                ws.write(row, 7, dout if dout else '-',
                         (fmt_dt_odd if odd else fmt_dt_even) if dout else fmt_dash)

                status = (l.status_type or 'UNKNOWN').upper().strip()
                sc = STATUS_COLORS.get(status, {"bg": "475569", "fg": "FFFFFF"})
                fmt_st = _fmt({'bold':True,'font_size':8,'align':'center','valign':'vcenter',
                                'bg_color':'#'+sc['bg'],'font_color':'#'+sc['fg'],
                                'border':1,'border_color':'#E2E8F0','font_name':'Arial'})
                ws.write(row, 8, status, fmt_st)
                ws.write(row, 9, l.pic or 'N/A', fmt_odd if odd else fmt_even)

            tot = len(logs) + 4
            ws.set_row(tot, 22)
            ws.merge_range(tot, 0, tot, 8, 'TOTAL RECORDS', fmt_tot_lbl)
            ws.write(tot, 9, len(logs), fmt_tot_val)

            # ── Sheet 2: Status Summary ──────────────────────────────
            ws2 = wb.add_worksheet('Status Summary')
            writer.sheets['Status Summary'] = ws2
            ws2.set_zoom(90)
            ws2.set_column(0, 0, 30); ws2.set_column(1, 1, 14); ws2.set_column(2, 2, 14)
            ws2.set_row(0, 36); ws2.set_row(1, 18); ws2.set_row(2, 6); ws2.set_row(3, 24)

            fmt_s_hdr = _fmt({'bold':True,'font_size':9,'font_color':'#FFFFFF','bg_color':'#1E293B','align':'center','valign':'vcenter','border':1,'border_color':'#334155','font_name':'Arial'})
            fmt_s_cnt = _fmt({'bold':True,'font_size':11,'align':'center','valign':'vcenter','bg_color':'#F8FAFC','border':1,'border_color':'#E2E8F0','font_name':'Arial'})
            fmt_s_pct = _fmt({'font_size':9,'align':'center','valign':'vcenter','bg_color':'#F1F5F9','font_color':'#64748B','border':1,'border_color':'#E2E8F0','font_name':'Arial','num_format':'0.0%'})
            fmt_s_sub = _fmt({'font_size':9,'font_color':'#94A3B8','bg_color':'#0F172A','font_name':'Arial'})
            fmt_s_tpct= _fmt({'bold':True,'font_size':10,'font_color':'#FBBF24','bg_color':'#0F172A','align':'center','valign':'vcenter','border':1,'border_color':'#334155','font_name':'Arial','num_format':'0.0%'})

            ws2.merge_range('A1:C1', '  STATUS SUMMARY', fmt_title)
            ws2.merge_range('A2:C2', f'  G7 Aerospace  -  {datetime.now().strftime("%d %B %Y")}', fmt_s_sub)
            ws2.merge_range('A3:C3', '', fmt_blank)
            ws2.write(3, 0, 'STATUS', fmt_s_hdr)
            ws2.write(3, 1, 'COUNT',  fmt_s_hdr)
            ws2.write(3, 2, '% OF TOTAL', fmt_s_hdr)

            sc_map = Counter((l.status_type or 'UNKNOWN').upper().strip() for l in logs)
            total  = len(logs)
            for ri, (status, count) in enumerate(sorted(sc_map.items(), key=lambda x: -x[1])):
                row = ri + 4
                ws2.set_row(row, 22)
                sc = STATUS_COLORS.get(status, {"bg": "475569", "fg": "FFFFFF"})
                fmt_lbl = _fmt({'bold':True,'font_size':9,'align':'left','valign':'vcenter',
                                 'bg_color':'#'+sc['bg'],'font_color':'#'+sc['fg'],
                                 'border':1,'border_color':'#E2E8F0','font_name':'Arial','indent':1})
                ws2.write(row, 0, status, fmt_lbl)
                ws2.write(row, 1, count, fmt_s_cnt)
                ws2.write(row, 2, count / total if total else 0, fmt_s_pct)

            s_row = len(sc_map) + 4
            ws2.set_row(s_row, 24)
            ws2.write(s_row, 0, 'GRAND TOTAL', fmt_tot_lbl)
            ws2.write(s_row, 1, total, fmt_tot_val)
            ws2.write(s_row, 2, 1.0, fmt_s_tpct)

            chart = wb.add_chart({'type': 'pie'})
            chart.add_series({
                'name': 'Status Distribution',
                'categories': [ws2.name, 4, 0, 3 + len(sc_map), 0],
                'values':     [ws2.name, 4, 1, 3 + len(sc_map), 1],
            })
            chart.set_title({'name': 'Repair Status Distribution'})
            chart.set_style(10)
            chart.set_size({'width': 420, 'height': 280})
            ws2.insert_chart(4, 4, chart, {'x_offset': 5, 'y_offset': 5})

            # ── Helper: write a log sheet ────────────────────────────
            def _write_log_sheet(wso, sheet_logs, banner, subtitle, ft, fs, fb, fh, fi, ftl, ftv):
                wso.set_zoom(90); wso.freeze_panes(4, 0)
                wso.set_row(0, 34); wso.set_row(1, 18); wso.set_row(2, 5); wso.set_row(3, 28)
                for i, w in enumerate(COL_W): wso.set_column(i, i, w)
                wso.merge_range('A1:J1', banner, ft)
                wso.merge_range('A2:J2', subtitle, fs)
                wso.merge_range('A3:J3', '', fb)
                for ci, col in enumerate(COLS): wso.write(3, ci, col, fh)
                for ri, l in enumerate(sheet_logs):
                    row = ri + 4; wso.set_row(row, 20); odd = ri % 2 == 0
                    wso.write(row, 0, l.id,                        fi)
                    wso.write(row, 1, l.drn or '-',                fmt_odd if odd else fmt_even)
                    wso.write(row, 2, l.peralatan or '-',          fmt_odd if odd else fmt_even)
                    wso.write(row, 3, l.pn or '-',                 fmt_pn_odd if odd else fmt_pn_even)
                    wso.write(row, 4, l.sn or '-',                 fmt_pn_odd if odd else fmt_pn_even)
                    wso.write(row, 5, l.defect or 'N/A',           fmt_def_odd if odd else fmt_def_even)
                    wso.write(row, 6, str(l.date_in) if l.date_in else '-', fmt_dt_odd if odd else fmt_dt_even)
                    dout = str(l.date_out) if l.date_out else None
                    wso.write(row, 7, dout if dout else '-', (fmt_dt_odd if odd else fmt_dt_even) if dout else fmt_dash)
                    status = (l.status_type or 'UNKNOWN').upper().strip()
                    sc2 = STATUS_COLORS.get(status, {"bg": "475569", "fg": "FFFFFF"})
                    fmt_st = _fmt({'bold':True,'font_size':8,'align':'center','valign':'vcenter',
                                    'bg_color':'#'+sc2['bg'],'font_color':'#'+sc2['fg'],
                                    'border':1,'border_color':'#E2E8F0','font_name':'Arial'})
                    wso.write(row, 8, status, fmt_st)
                    wso.write(row, 9, l.pic or 'N/A', fmt_odd if odd else fmt_even)
                tot_y = len(sheet_logs) + 4; wso.set_row(tot_y, 22)
                wso.merge_range(tot_y, 0, tot_y, 8, 'TOTAL RECORDS', ftl)
                wso.write(tot_y, 9, len(sheet_logs), ftv)

            # ── Per-year sheets ──────────────────────────────────────
            from collections import defaultdict
            year_map = defaultdict(list)
            for l in logs:
                yr = l.date_in.year if l.date_in else 0
                year_map[yr].append(l)

            YEAR_ACCENTS = [
                {'hdr_bg':'#1E3A5F','banner_bg':'#0F2340','tab':'#3B82F6'},
                {'hdr_bg':'#3B1F5E','banner_bg':'#1E0F40','tab':'#8B5CF6'},
                {'hdr_bg':'#1A4731','banner_bg':'#0A2818','tab':'#10B981'},
                {'hdr_bg':'#78350F','banner_bg':'#451A03','tab':'#F59E0B'},
                {'hdr_bg':'#7F1D1D','banner_bg':'#450A0A','tab':'#EF4444'},
                {'hdr_bg':'#164E63','banner_bg':'#083344','tab':'#06B6D4'},
            ]

            for yi, year in enumerate(sorted(year_map.keys(), reverse=True)):
                year_logs = year_map[year]
                acc = YEAR_ACCENTS[yi % len(YEAR_ACCENTS)]
                sheet_name = str(year) if year else 'Unknown'
                wsy = wb.add_worksheet(sheet_name)
                writer.sheets[sheet_name] = wsy
                wsy.set_tab_color(acc['tab'])
                _write_log_sheet(wsy, year_logs,
                    f'  G7 AEROSPACE  -  REPAIR LOG  {year}',
                    f'  Generated: {datetime.now().strftime("%d %B %Y  |  %H:%M")}   |   Records for {year}: {len(year_logs)}',
                    _fmt({'bold':True,'font_size':18,'font_color':'#FFFFFF','bg_color':acc['banner_bg'],'align':'left','valign':'vcenter','font_name':'Arial'}),
                    _fmt({'font_size':9,'font_color':'#94A3B8','bg_color':acc['banner_bg'],'align':'left','valign':'vcenter','font_name':'Arial'}),
                    _fmt({'bg_color':acc['banner_bg']}),
                    _fmt({'bold':True,'font_size':9,'font_color':'#FFFFFF','bg_color':acc['hdr_bg'],'align':'center','valign':'vcenter','border':1,'border_color':'#334155','text_wrap':True,'font_name':'Arial'}),
                    _fmt({'bold':True,'font_size':9,'align':'center','valign':'vcenter','bg_color':acc['hdr_bg'],'font_color':'#94A3B8','border':1,'border_color':'#334155','font_name':'Arial'}),
                    _fmt({'bold':True,'font_size':10,'font_color':'#FFFFFF','bg_color':acc['banner_bg'],'align':'right','valign':'vcenter','border':1,'border_color':'#334155','font_name':'Arial'}),
                    _fmt({'bold':True,'font_size':10,'font_color':'#FBBF24','bg_color':acc['banner_bg'],'align':'center','valign':'vcenter','border':1,'border_color':'#334155','font_name':'Arial'}),
                )

        output.seek(0)
        fname = f"G7_Repair_Log_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        return send_file(output,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                         as_attachment=True, download_name=fname)
    except Exception as e:
        logger.error(f"Excel Export Error: {e}")
        return f"Error Exporting Excel: {e}"


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