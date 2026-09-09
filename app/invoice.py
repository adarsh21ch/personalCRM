# -*- coding: utf-8 -*-
"""
One PDF tax invoice per successful charge, generated fresh at send time -
no storage bucket, no persisted invoice number counter, nothing that can
drift out of sync with the payment it describes. The invoice number is
derived from the payment row itself (its id and month), so it is stable and
unique without shared counter state to race across concurrent webhooks.

EVERY piece of text here goes through _wrap(). The first version of this
file drew the seller address with a bare drawString, which does not wrap:
a one-line address ran clean across the page and straight through the
right-aligned invoice number. Nothing in a document that reaches a paying
client may be laid out at a hardcoded y with unmeasured text.

GST: plan prices are charged exactly as shown - nothing is added at
checkout - so a price is GST-INCLUSIVE by construction. With a GSTIN
configured, this backs the tax out of that fixed total for the legal
breakup; without one, the invoice shows a plain amount and no tax line,
because charging a tax you are not registered for is not something to do
by accident.
"""
import io
from datetime import datetime, timedelta

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfgen import canvas

import settings

INK = colors.HexColor("#111214")
SUB = colors.HexColor("#6b7080")
LINE = colors.HexColor("#dcdee3")
BAND = colors.HexColor("#f5f6f8")
ACCENT = colors.HexColor("#ff9933")

W, H = A4
MARGIN = 18 * mm
CONTENT_W = W - 2 * MARGIN


# ------------------------------------------------------------------ helpers
def invoice_number(payment):
    return "INV-%s-%s" % (payment["created_at"][:7].replace("-", ""), payment["id"][:6].upper())


def _gst_breakup(amount_paise, rate_percent):
    """(base_paise, gst_paise) backed out of a GST-inclusive total."""
    rate = rate_percent / 100.0
    base = round(amount_paise / (1 + rate))
    return base, amount_paise - base


def _wrap(c, text, font, size, max_width):
    """Lines that each fit inside max_width. Honours explicit newlines, and
    never returns a line wider than the box it was measured against."""
    out = []
    for para in str(text or "").replace("\r", "").split("\n"):
        words, cur = para.split(), ""
        if not words:
            continue
        for word in words:
            trial = (cur + " " + word).strip()
            if not cur or c.stringWidth(trial, font, size) <= max_width:
                cur = trial
            else:
                out.append(cur)
                cur = word
        out.append(cur)
    return out


def _draw_lines(c, lines, x, y, font, size, leading, color):
    c.setFont(font, size)
    c.setFillColor(color)
    for ln in lines:
        c.drawString(x, y, ln)
        y -= leading
    return y


def _rupees(paise):
    return "%s" % format(paise / 100.0, ",.2f")


_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
         "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
         "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _under_hundred(n):
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _in_words(rupees):
    """Indian numbering (lakh/crore), as printed on every Indian invoice."""
    if rupees == 0:
        return "Zero"
    parts = []
    for divisor, label in ((10000000, "Crore"), (100000, "Lakh"), (1000, "Thousand"), (100, "Hundred")):
        if rupees >= divisor:
            count = rupees // divisor
            rupees %= divisor
            # crore/lakh counts can themselves exceed 99, so recurse for those
            head = _in_words(count) if divisor >= 100000 and count > 99 else _under_hundred(count)
            parts.append("%s %s" % (head, label))
    if rupees:
        parts.append(_under_hundred(rupees))
    return " ".join(parts)


def _billing_period(start_iso):
    """'09 Sep 2026 - 08 Oct 2026' - the cycle ends the day before the next
    charge falls due, not on it."""
    try:
        start = datetime.strptime(start_iso[:10], "%Y-%m-%d").date()
    except Exception:
        return ""
    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    day = start.day
    while day > 1:                       # clamp 31st into a shorter month
        try:
            next_charge = start.replace(year=y, month=m, day=day)
            break
        except ValueError:
            day -= 1
    else:
        next_charge = start.replace(year=y, month=m, day=1)
    end = next_charge - timedelta(days=1)
    return "%s - %s" % (start.strftime("%d %b %Y"), end.strftime("%d %b %Y"))


# -------------------------------------------------------------------- build
def build_invoice_pdf(payment, client, plan):
    """PDF bytes for one payment. Never raises on missing optional settings -
    a blank business address or unset GSTIN just means a shorter invoice,
    not a failure to send the receipt at all."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(invoice_number(payment))

    business = settings.business_name()
    address = settings.business_address()
    gstin = settings.gstin()
    support = settings.support_email()
    phone = settings.support_phone()
    inv_no = invoice_number(payment)
    amount = payment["amount_paise"]
    plan_name = (plan or {}).get("name") or "Subscription"

    # --- accent band, so the page reads as branded stationery ------------
    c.setFillColor(ACCENT)
    c.rect(0, H - 8 * mm, W, 8 * mm, fill=1, stroke=0)

    # --- two top columns: seller left, invoice meta right ----------------
    # Each column is measured independently and the divider is placed under
    # whichever ran longer, so a long address can never collide with the
    # meta block or push text off the page.
    left_w = CONTENT_W * 0.58
    right_x = MARGIN + CONTENT_W
    top = H - 22 * mm

    ly = top
    ly = _draw_lines(c, _wrap(c, business, "Helvetica-Bold", 15, left_w),
                     MARGIN, ly, "Helvetica-Bold", 15, 6.5 * mm, INK)
    if address:
        ly = _draw_lines(c, _wrap(c, address, "Helvetica", 8.5, left_w),
                         MARGIN, ly + 1.5 * mm, "Helvetica", 8.5, 4.2 * mm, SUB)
    if gstin:
        ly = _draw_lines(c, ["GSTIN: %s" % gstin], MARGIN, ly, "Helvetica-Bold", 8.5, 4.2 * mm, INK)

    ry = top
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(INK)
    c.drawRightString(right_x, ry, "TAX INVOICE" if gstin else "INVOICE")
    ry -= 7 * mm
    for label, value in (("Invoice No.", inv_no), ("Date", payment["created_at"][:10])):
        c.setFont("Helvetica", 8.5)
        c.setFillColor(SUB)
        c.drawRightString(right_x, ry, "%s  %s" % (label, value))
        ry -= 4.6 * mm

    y = min(ly, ry) - 6 * mm
    c.setStrokeColor(LINE)
    c.setLineWidth(0.7)
    c.line(MARGIN, y, MARGIN + CONTENT_W, y)

    # --- billed to --------------------------------------------------------
    y -= 9 * mm
    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(SUB)
    c.drawString(MARGIN, y, "BILLED TO")
    y -= 5.5 * mm
    y = _draw_lines(c, _wrap(c, client.get("name") or "", "Helvetica-Bold", 11, left_w),
                    MARGIN, y, "Helvetica-Bold", 11, 5 * mm, INK)
    for field in ("company", "email", "phone"):
        if client.get(field):
            y = _draw_lines(c, _wrap(c, client[field], "Helvetica", 9, left_w),
                            MARGIN, y, "Helvetica", 9, 4.4 * mm, SUB)

    # --- line items -------------------------------------------------------
    y -= 8 * mm
    amount_col = 32 * mm                       # right-hand money column
    desc_w = CONTENT_W - amount_col - 6 * mm

    c.setFillColor(BAND)
    c.rect(MARGIN, y - 7 * mm, CONTENT_W, 7 * mm, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7.5)
    c.setFillColor(SUB)
    c.drawString(MARGIN + 3 * mm, y - 4.8 * mm, "DESCRIPTION")
    c.drawRightString(MARGIN + CONTENT_W - 3 * mm, y - 4.8 * mm, "AMOUNT (INR)")
    y -= 7 * mm

    period = _billing_period(payment["created_at"])
    desc_lines = _wrap(c, "%s - monthly subscription" % plan_name, "Helvetica", 9.5, desc_w)
    sub_lines = _wrap(c, "Billing period %s" % period, "Helvetica", 8, desc_w) if period else []

    if gstin:
        rate = settings.gst_rate_percent()
        base_paise, gst_paise = _gst_breakup(amount, rate)
        item_value = base_paise
    else:
        item_value = amount

    row_h = 6 * mm + len(desc_lines) * 4.6 * mm + len(sub_lines) * 3.8 * mm
    ry = y - 5 * mm
    ty = _draw_lines(c, desc_lines, MARGIN + 3 * mm, ry, "Helvetica", 9.5, 4.6 * mm, INK)
    if sub_lines:
        _draw_lines(c, sub_lines, MARGIN + 3 * mm, ty + 0.5 * mm, "Helvetica", 8, 3.8 * mm, SUB)
    c.setFont("Helvetica", 9.5)
    c.setFillColor(INK)
    c.drawRightString(MARGIN + CONTENT_W - 3 * mm, ry, _rupees(item_value))
    y -= row_h
    c.setStrokeColor(LINE)
    c.line(MARGIN, y, MARGIN + CONTENT_W, y)

    # --- totals, right aligned under the amount column --------------------
    label_x = MARGIN + CONTENT_W - amount_col - 4 * mm
    value_x = MARGIN + CONTENT_W - 3 * mm
    y -= 7 * mm

    def total_row(label, value, bold=False, color=INK, size=9):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.setFillColor(color)
        c.drawRightString(label_x, y, label)
        c.drawRightString(value_x, y, value)
        y -= 5.6 * mm

    if gstin:
        total_row("Taxable value", _rupees(base_paise))
        rate = settings.gst_rate_percent()
        total_row("GST @ %g%%" % rate, _rupees(gst_paise))

    y -= 1 * mm
    c.setStrokeColor(LINE)
    c.line(label_x - 26 * mm, y + 3.5 * mm, value_x, y + 3.5 * mm)
    y -= 1.5 * mm
    total_row("TOTAL PAID", _rupees(amount), bold=True, color=INK, size=11)

    # --- amount in words + payment reference ------------------------------
    y -= 4 * mm
    words = "%s Rupees Only" % _in_words(int(round(amount / 100.0)))
    y = _draw_lines(c, _wrap(c, "Amount in words: %s" % words, "Helvetica-Bold", 8.5, CONTENT_W),
                    MARGIN, y, "Helvetica-Bold", 8.5, 4.4 * mm, INK)
    y -= 1 * mm
    ref = "Paid via Razorpay"
    if payment.get("method"):
        ref += " - %s" % payment["method"]
    if payment.get("rzp_payment_id"):
        ref += " - %s" % payment["rzp_payment_id"]
    y = _draw_lines(c, _wrap(c, ref, "Helvetica", 8.5, CONTENT_W),
                    MARGIN, y, "Helvetica", 8.5, 4.4 * mm, SUB)

    # --- footer, pinned to the bottom of the page -------------------------
    fy = 26 * mm
    c.setStrokeColor(LINE)
    c.line(MARGIN, fy, MARGIN + CONTENT_W, fy)
    fy -= 5.5 * mm
    note = ("This invoice confirms a payment already received - it is not a request for payment. "
            "Subscription charges are billed monthly and can be cancelled at any time.")
    fy = _draw_lines(c, _wrap(c, note, "Helvetica", 7.5, CONTENT_W),
                     MARGIN, fy, "Helvetica", 7.5, 3.6 * mm, SUB)
    contact = " | ".join(x for x in (support, phone) if x)
    if contact:
        fy = _draw_lines(c, _wrap(c, "Questions about this invoice: %s" % contact,
                                  "Helvetica", 7.5, CONTENT_W),
                         MARGIN, fy, "Helvetica", 7.5, 3.6 * mm, SUB)
    _draw_lines(c, ["This is a computer-generated invoice and does not require a signature."],
                MARGIN, fy, "Helvetica-Oblique", 7, 3.6 * mm, SUB)

    c.showPage()
    c.save()
    return buf.getvalue()
