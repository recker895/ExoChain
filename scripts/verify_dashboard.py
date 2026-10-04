"""One browser verification against the production build and real recorded run."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from config.settings import settings


def main():
    report = json.loads(Path("data/api_live_verification.json").read_text())
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1050})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("http://127.0.0.1:3000", wait_until="domcontentloaded")
        page.get_by_role("button", name="Access settings", exact=True).click()
        page.locator('input[type="password"]').first.fill(settings.OPERATOR_API_TOKEN.get_secret_value())
        page.get_by_role("button", name="Close access settings", exact=True).click()
        selector = page.get_by_role("combobox", name="Current run")
        page.wait_for_function("id => [...document.querySelectorAll('select option')].some(o => o.value === id)", arg=report["run_id"])
        selector.select_option(report["run_id"])
        page.get_by_text("Business evidence required", exact=True).wait_for()
        page.wait_for_function("() => !document.querySelector('.health-time')?.textContent.includes('Backend not connected')")
        page.locator('.maplibregl-canvas').wait_for()
        page.wait_for_function("() => Number(document.querySelector('[data-rendered-vessels]')?.dataset.renderedVessels) > 0")
        rendered = int(page.locator('[data-rendered-vessels]').get_attribute('data-rendered-vessels'))
        page.screenshot(path="data/dashboard-command-center.png", full_page=True)
        views = ["Live Operations", "Intelligence", "Decisions", "Optimization", "Scenarios / What-If", "Agents", "Audit Trail", "Data Sources", "System Health"]
        for view in views:
            page.get_by_role("button", name=view, exact=True).click()
            page.get_by_role("heading", level=1, name=view, exact=True).wait_for()
            assert page.locator('main').inner_text().strip()
            if view == "Agents":
                assert page.locator('.matrix-agent').count() == 19
            if view == "Scenarios / What-If":
                assert page.get_by_text("Scenario unavailable — required business data missing.", exact=True).is_visible()
        page.get_by_role("button", name="Command Center", exact=True).click()
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="data/dashboard-mobile.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Horizontal overflow on mobile"
        assert not errors, errors
        browser.close()
    result = {"run_id": report["run_id"], "views_checked": 10, "agent_cards": 19, "rendered_vessels": rendered, "browser_errors": errors, "mobile_overflow": False, "backend_state": report["status"]}
    Path("data/dashboard_verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
