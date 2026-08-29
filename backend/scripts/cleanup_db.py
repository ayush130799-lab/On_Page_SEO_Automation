"""Cleanup script to deactivate invalid loop/parameter pages and re-sync metrics."""

from app.db import SessionLocal
from app.models import Page, Website, GA4Metric
from app.utils.url_utils import has_recursive_path_loop, is_probably_page
from app.services.pipeline import refresh_website_summary
from app.services.integrations import ga4
import asyncio

def cleanup_pages():
    db = SessionLocal()
    try:
        websites = db.query(Website).all()
        for w in websites:
            pages = db.query(Page).filter(Page.website_id == w.id).all()
            deactivated = 0
            for p in pages:
                if has_recursive_path_loop(p.url) or not is_probably_page(p.url):
                    p.is_active = False
                    deactivated += 1
            db.commit()
            refresh_website_summary(db, w)
            db.commit()
            print(f"Website {w.id} ({w.name}): deactivated {deactivated} stale/loop pages. Remaining active: {w.total_pages}")
            
            # Re-sync GA4 metrics
            try:
                summary = asyncio.run(ga4.sync(db, w))
                print(f"GA4 sync for {w.name}: {summary}")
            except Exception as e:
                print(f"GA4 sync error for {w.name}: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    cleanup_pages()
