import io
from datetime import datetime
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from ..models import Audit, Page, Issue, Recommendation

def generate_audit_pdf(audit: Audit, pages: list[Page], issues: list[Issue], recommendations: list[Recommendation]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontSize=22,
        leading=26,
        textColor=colors.HexColor('#0f172a'),
        spaceAfter=6,
    )
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontSize=10,
        textColor=colors.HexColor('#64748b'),
        spaceAfter=14,
    )
    section_heading = ParagraphStyle(
        'SectionHeading',
        parent=styles['Heading2'],
        fontSize=14,
        leading=18,
        textColor=colors.HexColor('#1e293b'),
        spaceBefore=12,
        spaceAfter=8,
    )
    cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor('#334155'),
    )
    cell_bold = ParagraphStyle(
        'TableCellBold',
        parent=styles['Normal'],
        fontSize=8,
        leading=10,
        fontName='Helvetica-Bold',
        textColor=colors.HexColor('#0f172a'),
    )

    story = []

    # Title & Metadata
    domain = audit.website.domain if audit.website else "Website"
    audit_date = audit.completed_at.strftime("%Y-%m-%d %H:%M UTC") if audit.completed_at else datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    
    story.append(Paragraph(f"SEO Audit Report: {domain}", title_style))
    story.append(Paragraph(f"Audit #{audit.id} | Generated: {audit_date} | Target URL: {audit.website.url if audit.website else 'N/A'}", subtitle_style))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cbd5e1"), spaceAfter=14))

    # High-level Summary Metrics Table
    scores = [p.seo_score for p in pages if p.seo_score is not None]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0
    overall_score = audit.overall_score if audit.overall_score is not None else avg_score
    
    high_issues = sum(1 for i in issues if i.severity == "high")
    med_issues = sum(1 for i in issues if i.severity == "medium")
    low_issues = sum(1 for i in issues if i.severity == "low")

    summary_data = [
        [
            Paragraph("<b>Overall SEO Score</b>", cell_style),
            Paragraph("<b>Total Crawled Pages</b>", cell_style),
            Paragraph("<b>Average Page Score</b>", cell_style),
            Paragraph("<b>High Issues</b>", cell_style),
            Paragraph("<b>Medium Issues</b>", cell_style),
        ],
        [
            Paragraph(f"<font size=14 color='#0f766e'><b>{overall_score}/100</b></font>", cell_style),
            Paragraph(f"<font size=14><b>{len(pages)}</b></font>", cell_style),
            Paragraph(f"<font size=14><b>{avg_score}/100</b></font>", cell_style),
            Paragraph(f"<font size=14 color='#b91c1c'><b>{high_issues}</b></font>", cell_style),
            Paragraph(f"<font size=14 color='#b45309'><b>{med_issues}</b></font>", cell_style),
        ]
    ]
    summary_table = Table(summary_data, colWidths=[108, 108, 108, 108, 108])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#e2e8f0')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 14))

    # Discovered Issues Table
    story.append(Paragraph("Identified SEO Issues", section_heading))
    if issues:
        issues_data = [[
            Paragraph("<b>Severity</b>", cell_bold),
            Paragraph("<b>Issue Type</b>", cell_bold),
            Paragraph("<b>Description</b>", cell_bold),
            Paragraph("<b>Source</b>", cell_bold)
        ]]
        for issue in issues[:25]:
            sev_color = "#b91c1c" if issue.severity == "high" else "#b45309" if issue.severity == "medium" else "#475569"
            issues_data.append([
                Paragraph(f"<font color='{sev_color}'><b>{issue.severity.upper()}</b></font>", cell_style),
                Paragraph(issue.issue_type, cell_style),
                Paragraph(issue.description, cell_style),
                Paragraph(issue.source, cell_style),
            ])
        issues_table = Table(issues_data, colWidths=[65, 120, 275, 80])
        issues_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(issues_table)
    else:
        story.append(Paragraph("No critical issues detected.", cell_style))

    story.append(Spacer(1, 14))

    # Page-Level Breakdown Table
    story.append(Paragraph("Crawled Page Breakdown", section_heading))
    if pages:
        pages_data = [[
            Paragraph("<b>Page URL</b>", cell_bold),
            Paragraph("<b>HTTP</b>", cell_bold),
            Paragraph("<b>Score</b>", cell_bold),
            Paragraph("<b>Title</b>", cell_bold),
            Paragraph("<b>Images / Missing Alt</b>", cell_bold),
        ]]
        for p in pages[:30]:
            score_color = "#15803d" if (p.seo_score or 0) >= 80 else "#b45309" if (p.seo_score or 0) >= 60 else "#b91c1c"
            pages_data.append([
                Paragraph(p.url, cell_style),
                Paragraph(str(p.status_code or "—"), cell_style),
                Paragraph(f"<font color='{score_color}'><b>{p.seo_score or '—'}</b></font>", cell_style),
                Paragraph(p.title[:50] + "..." if p.title and len(p.title) > 50 else (p.title or "Missing"), cell_style),
                Paragraph(f"{p.image_count} ({p.missing_alt_count} missing)", cell_style),
            ])
        pages_table = Table(pages_data, colWidths=[180, 35, 45, 180, 100])
        pages_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f1f5f9')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#cbd5e1')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(pages_table)

    story.append(Spacer(1, 14))

    # Top Recommendations
    story.append(Paragraph("Actionable Recommendations & Fixes", section_heading))
    if recommendations:
        for idx, rec in enumerate(recommendations[:10], start=1):
            story.append(Paragraph(f"<b>{idx}. {rec.recommendation}</b>", cell_bold))
            if rec.suggested_fix:
                story.append(Paragraph(f"<font color='#334155'>Fix: {rec.suggested_fix}</font>", cell_style))
            story.append(Spacer(1, 4))
    else:
        story.append(Paragraph("No recommendations generated.", cell_style))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()
