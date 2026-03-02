import argparse
import hashlib
import os
import re
import sys
import time
from datetime import datetime
from typing import Callable
from urllib.parse import urljoin, urlparse

import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from kategorije import promjenaKategorije

BASE_URL = "https://www.b2b.gama-electronic.com"
LOGIN_URL = f"{BASE_URL}/prijava"
OUTPUT_COLUMNS = ["Kategorija", "Proizvodjac", "Naslov", "Opis", "Cijena", "Slike", "Link"]
MIN_PRICE = 0.1

USERNAME = os.getenv("GAMA_USERNAME", "info@tehnomax.ba")
PASSWORD = os.getenv("GAMA_PASSWORD", "sp15zv1")


def get_default_geckodriver_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "geckodriver.exe"))
        candidates.append(os.path.join(exe_dir, "_internal", "geckodriver.exe"))

    candidates.append(os.path.join(here, "..", "geckodriver.exe"))
    candidates.append(os.path.join(here, "geckodriver.exe"))

    for candidate in candidates:
        normalized = os.path.normpath(candidate)
        if os.path.exists(normalized):
            return normalized

    return os.path.normpath(candidates[0])


def build_driver(geckodriver_path: str | None = None, headless: bool = False) -> webdriver.Firefox:
    service = Service(executable_path=geckodriver_path or get_default_geckodriver_path())
    options = webdriver.FirefoxOptions()
    options.headless = headless
    driver = webdriver.Firefox(service=service, options=options)
    driver.set_page_load_timeout(600)
    driver.maximize_window()
    return driver


def safe_text(ctx: WebDriver | WebElement, selector: str, by: By = By.CSS_SELECTOR) -> str | None:
    try:
        return ctx.find_element(by, selector).text.strip()
    except Exception:
        return None


def parse_price(raw_text: str | None) -> str | None:
    if not raw_text:
        return None
    no_dots = raw_text.replace(".", "")
    normalized = no_dots.replace(",", ".")
    match = re.search(r"\d+\.?\d*", normalized)
    return match.group(0) if match else None


def is_zero_like(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (int, float)):
        try:
            return float(value) == 0.0
        except Exception:
            return False
    text = str(value).strip()
    if not text:
        return False
    compact = re.sub(r"\s+", "", text)
    return bool(re.fullmatch(r"0+([.,]0+)?", compact))


def clean_zero_like(value):
    return None if is_zero_like(value) else value


def is_positive_price(price: str | None) -> bool:
    if price is None:
        return False
    try:
        return float(price) >= MIN_PRICE
    except (TypeError, ValueError):
        return False


def normalize_category_url(href: str | None, base_hint: str | None = None) -> str | None:
    if not href:
        return None
    absolute = urljoin(base_hint or BASE_URL, href).split("#")[0].strip()
    if not absolute.startswith(BASE_URL):
        return None

    parsed = urlparse(absolute)
    path = (parsed.path or "").rstrip("/")

    if "/proizvod/" in path:
        return None
    if not path.startswith("/proizvodi/"):
        return None
    if path.startswith("/proizvodi/rasprodaja"):
        return None
    if parsed.query:
        return None

    return f"{BASE_URL}{path}"


def normalized_category_urls_from_raw(raw_value: str | None, base_hint: str | None = None) -> list[str]:
    if not raw_value:
        return []

    candidates = [raw_value.strip()]
    for pattern in (r"https?://[^\s\"'<>]+", r"/proizvodi[^\s\"'<>]*"):
        for match in re.findall(pattern, raw_value):
            if match:
                candidates.append(match.strip())

    out = []
    seen = set()
    for candidate in candidates:
        normalized = normalize_category_url(candidate, base_hint=base_hint)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def category_segments(url: str) -> list[str]:
    path = (urlparse(url).path or "").strip("/")
    parts = path.split("/")
    if not parts or parts[0] != "proizvodi":
        return []
    return [part for part in parts[1:] if part]


def category_depth(url: str) -> int:
    return len(category_segments(url))


def is_direct_child(parent_url: str, candidate_url: str) -> bool:
    parent = category_segments(parent_url)
    child = category_segments(candidate_url)
    if not parent or not child:
        return False
    return child[: len(parent)] == parent and len(child) == len(parent) + 1


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\\\|?*]+', "-", (name or "").strip())
    cleaned = re.sub(r"\s+", "-", cleaned).strip(" .-")
    return cleaned or "KATEGORIJA"


def _is_authenticated_view(driver: webdriver.Firefox) -> bool:
    if "/prijava" in (driver.current_url or "").lower():
        return False
    selectors = [
        (By.CLASS_NAME, "main_menu_wrap"),
        (By.CSS_SELECTOR, "section.main-header"),
        (By.CSS_SELECTOR, "a[href*='/odjava']"),
        (By.CSS_SELECTOR, "a[href*='/proizvodi']"),
    ]
    for by, value in selectors:
        try:
            if driver.find_elements(by, value):
                return True
        except Exception:
            continue
    return False


def wait_for_login(driver: webdriver.Firefox) -> None:
    WebDriverWait(driver, 60).until(lambda d: _is_authenticated_view(d))


def handle_cookie_overlay(driver: webdriver.Firefox) -> None:
    selectors = [
        "button.accept-cookies",
        "button.reject-all-btn",
        "button.accept-all-btn",
        ".cookies_content button",
        ".cookies_bg + div button",
    ]
    for selector in selectors:
        try:
            button = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
            button.click()
            time.sleep(0.2)
            break
        except Exception:
            continue

    try:
        driver.execute_script(
            """
            const bg = document.querySelector('.cookies_bg');
            if (bg) bg.style.display = 'none';
            const box = document.querySelector('.cookies_content');
            if (box) box.style.display = 'none';
            """
        )
    except Exception:
        pass


def login(driver: webdriver.Firefox, username: str, password: str) -> None:
    driver.get(LOGIN_URL)
    handle_cookie_overlay(driver)

    try:
        open_login = driver.find_element(By.CSS_SELECTOR, ".open-login")
        if open_login.is_displayed():
            open_login.click()
            time.sleep(0.2)
    except Exception:
        pass

    login_form = None
    for selector in ("#form-login2", "#form-login", "form#form-login2", "form#form-login"):
        try:
            forms = driver.find_elements(By.CSS_SELECTOR, selector)
            for form in forms:
                if (
                    form.find_elements(By.CSS_SELECTOR, ".email")
                    and form.find_elements(By.CSS_SELECTOR, ".password")
                    and form.find_elements(By.CSS_SELECTOR, ".btn-login")
                ):
                    login_form = form
                    break
            if login_form is not None:
                break
        except Exception:
            continue

    if login_form is None:
        raise RuntimeError("Login form was not found on /prijava.")

    username_input = login_form.find_element(By.CSS_SELECTOR, ".email")
    password_input = login_form.find_element(By.CSS_SELECTOR, ".password")
    submit_button = login_form.find_element(By.CSS_SELECTOR, ".btn-login")

    username_input.clear()
    username_input.send_keys(username)
    password_input.clear()
    password_input.send_keys(password)

    handle_cookie_overlay(driver)
    try:
        submit_button.click()
    except Exception:
        driver.execute_script("arguments[0].click();", submit_button)

    wait_for_login(driver)


def collect_category_links_from_current_page(driver: webdriver.Firefox) -> list[str]:
    found = []
    seen = set()

    def _add(href: str | None) -> None:
        if href and href not in seen:
            seen.add(href)
            found.append(href)

    try:
        anchors = driver.find_elements(By.XPATH, "//a[@href and not(starts-with(@href, 'javascript:'))]")
        for anchor in anchors:
            try:
                raw = anchor.get_attribute("href")
            except Exception:
                continue
            for href in normalized_category_urls_from_raw(raw, base_hint=driver.current_url):
                _add(href)
    except Exception:
        pass

    try:
        raw_candidates = driver.execute_script(
            """
            const out = [];
            const seen = new Set();
            for (const el of document.querySelectorAll('a, [data-href], [data-url]')) {
                const values = [
                    el.getAttribute('href'),
                    el.getAttribute('data-href'),
                    el.getAttribute('data-url'),
                    el.getAttribute('onclick')
                ];
                for (const value of values) {
                    if (!value) continue;
                    const trimmed = value.trim();
                    if (!trimmed || seen.has(trimmed)) continue;
                    seen.add(trimmed);
                    out.push(trimmed);
                }
            }
            return out;
            """
        )
        if raw_candidates:
            for raw in raw_candidates:
                for href in normalized_category_urls_from_raw(raw, base_hint=driver.current_url):
                    _add(href)
    except Exception:
        pass

    try:
        html = driver.page_source or ""
        for raw in re.findall(r"https?://[^\s\"'<>]+|/proizvodi[^\s\"'<>]*", html):
            for href in normalized_category_urls_from_raw(raw, base_hint=driver.current_url):
                _add(href)
    except Exception:
        pass

    return found


def collect_seed_categories(driver: webdriver.Firefox) -> list[str]:
    seeds = []
    seen = set()
    for url in (f"{BASE_URL}/proizvodi", BASE_URL):
        try:
            driver.get(url)
            try:
                WebDriverWait(driver, 8).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, "a[href*='/proizvodi/']")) > 0
                )
            except Exception:
                time.sleep(1)
            for href in collect_category_links_from_current_page(driver):
                if href not in seen:
                    seen.add(href)
                    seeds.append(href)
        except Exception:
            continue
    return seeds


def discover_categories(driver: webdriver.Firefox) -> list[str]:
    pending = list(collect_seed_categories(driver))
    seen = set()
    discovered = []

    while pending:
        category_url = normalize_category_url(pending.pop(0))
        if not category_url or category_url in seen:
            continue
        seen.add(category_url)
        discovered.append(category_url)

        try:
            driver.get(category_url)
            time.sleep(0.35)
        except Exception:
            continue

        for child in collect_category_links_from_current_page(driver):
            child = normalize_category_url(child)
            if not child:
                continue
            if not is_direct_child(category_url, child):
                continue
            if child not in seen and child not in pending:
                pending.append(child)

    # Parse ONLY deep subcategories:
    # skip main category (depth 1) and first subcategory (depth 2).
    deep_subcategories = [url for url in discovered if category_depth(url) >= 2]
    return deep_subcategories


def scroll_until_products_stop(
    driver: webdriver.Firefox,
    product_selector: str = ".figure-grid",
    pause: float = 0.8,
    max_scrolls: int = 80,
    stable_rounds: int = 4,
) -> int:
    """
    Scrolls until the count of product cards stops increasing for stable_rounds checks.
    Returns final count.
    """
    last_count = 0
    stable = 0

    for _ in range(max_scrolls):
        cards = driver.find_elements(By.CSS_SELECTOR, product_selector)
        count = len(cards)

        if count > last_count:
            last_count = count
            stable = 0
        else:
            stable += 1

        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(pause)
        driver.execute_script("window.scrollBy(0, 400);")
        time.sleep(0.15)

        if stable >= stable_rounds:
            break

    return last_count


def current_category_text(driver: webdriver.Firefox) -> str:
    try:
        return driver.find_element(
            By.CSS_SELECTOR,
            "body > div.wrapper > section.main-header > header > div > ol > li:last-child",
        ).text.strip()
    except Exception:
        return ""
def extract_stock_qty(driver: webdriver.Firefox) -> int | None:
    xpaths = [
        # najčešće je u add-to boxu
        "//*[contains(@class,'info-box-addto')]//*[contains(translate(., 'KOM', 'kom'), 'kom')]",
        # ili u product info dijelu
        "//*[contains(@class,'product-flex-info')]//*[contains(translate(., 'KOM', 'kom'), 'kom')]",
    ]

    for xp in xpaths:
        try:
            els = driver.find_elements(By.XPATH, xp)
        except Exception:
            continue

        for el in els:
            txt = (el.text or "").strip().lower()
            if "kom" not in txt:
                continue
            # hvataj samo obrasce tipa "0 kom." / "15 kom."
            m = re.search(r"\b(\d+)\s*kom\b", txt.replace(".", ""))
            if m:
                return int(m.group(1))

    return None


def extract_price_text_detail(driver: webdriver.Firefox) -> str | None:
    selectors = [
        # tvoj stari
        "div.info-box.info-box-addto div.price",
        "div.info-box.info-box-addto span",
        # “nova” struktura sa price-big / price-dec-symb
        ".price-big",
        ".price-dec-symb",
        ".price-box",
        ".price",
        "span.h3",
    ]

    texts = []
    seen = set()

    for css in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, css)
        except Exception:
            continue
        for el in els:
            t = (el.text or "").strip()
            if not t or t in seen:
                continue
            seen.add(t)
            texts.append(t)

    # spoji u jedan string (jer nekad je 30,90 u jednom divu a KM u drugom)
    joined = " ".join(texts)

    # mora imati KM i broj
    if "km" in joined.lower() and re.search(r"\d", joined):
        return joined

    # fallback: probaj sve elemente na stranici s KM (skuplje ali radi)
    try:
        km_nodes = driver.find_elements(By.XPATH, "//*[contains(translate(., 'KM', 'km'), 'km')]")
        for el in km_nodes[:50]:
            t = (el.text or "").strip()
            if "km" in t.lower() and parse_price(t):
                return t
    except Exception:
        pass

    return None

def get_listing_products(
    driver: webdriver.Firefox,
    skip_log: Callable[[str], None] | None = None,
    category_name: str | None = None,
) -> list[dict]:
    products = []
    try:
        WebDriverWait(driver, 12).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".figure-grid"))
        )
    except Exception:
        if skip_log:
            skip_log(f"[listing] no product cards loaded | category={category_name or ''} | url={driver.current_url}")
        return products

    cards = driver.find_elements(By.CSS_SELECTOR, ".figure-grid")
    for index, card in enumerate(cards, start=1):
        try:
            link = card.find_element(By.TAG_NAME, "a").get_attribute("href")

            title = clean_zero_like(
                safe_text(card, ".//div[contains(@class, 'text')]/h2/a", by=By.XPATH)
            )
            if not title:
                if skip_log:
                    skip_log(
                        f"[skip] listing_no_title | category={category_name or ''} | link={link or ''} | card_index={index}"
                    )
                continue

            products.append({"link": link, "title": title})
        except Exception as exc:
            if skip_log:
                skip_log(
                    f"[skip] listing_parse_exception | category={category_name or ''} | card_index={index} | error={exc}"
                )
            continue

    return products



def extract_images(driver: webdriver.Firefox) -> str:
    images = []
    for img in driver.find_elements(By.CLASS_NAME, "fancyboxgallery"):
        href = img.get_attribute("href")
        if href == f"{BASE_URL}/images/no-image.png":
            images.append("")
        elif href:
            images.append(href)
    return " ".join(images)


def extract_manufacturer(driver: webdriver.Firefox) -> str | None:
    try:
        info_boxes = driver.find_elements(
            By.XPATH,
            "//div[@class='info-box' and not(contains(@class, 'info-box-addto'))]",
        )
        if not info_boxes:
            return None
        spans = info_boxes[0].find_elements(By.TAG_NAME, "span")
        return clean_zero_like(spans[1].text.strip()) if len(spans) > 1 else None
    except Exception:
        return None


def extract_description(driver: webdriver.Firefox) -> str:
    manufacturer_block = ""
    model_block = ""
    barcode_block = ""

    try:
        block = driver.find_element(
            By.CSS_SELECTOR,
            "body > div.wrapper > section > div.main > div > div > div.col-md-6.col-sm-12.product-flex-info > div > div > div:nth-child(3)",
        ).text
        manufacturer_block = block.replace("\n", ":")
    except Exception:
        pass

    try:
        block = driver.find_element(
            By.CSS_SELECTOR,
            "body > div.wrapper > section > div.main > div > div > div.col-md-6.col-sm-12.product-flex-info > div > div > div:nth-child(4)",
        ).text
        model_block = block.replace("\n", ":")
    except Exception:
        pass

    try:
        block = driver.find_element(
            By.CSS_SELECTOR,
            "body > div.wrapper > section > div.main > div > div > div.col-md-6.col-sm-12.product-flex-info > div > div > div:nth-child(5)",
        ).text
        barcode_block = block.replace("\n", ":")
    except Exception:
        pass

    transformed_html = ""
    try:
        content_div = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "div.content"))
        )
        driver.execute_script("arguments[0].scrollIntoView(true);", content_div)
        transformed_html = driver.execute_script(
            """
            const contentDiv = arguments[0];
            let description = '';
            const pTags = contentDiv.querySelectorAll('p');
            if (pTags.length > 0) {
                pTags.forEach(p => {
                    const parts = p.innerHTML.split('<br>').filter(part => part.trim() !== '');
                    parts.forEach(part => {
                        if (part.trim()) description += `<p>${part.trim()}</p>`;
                    });
                });
            } else {
                let segments = [];
                contentDiv.childNodes.forEach(node => {
                    if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                        segments.push(node.textContent.trim());
                    } else if (node.nodeName.toLowerCase() === 'br') {
                        segments.push('');
                    }
                });
                segments.forEach(segment => {
                    if (segment) description += `<p>${segment}</p>`;
                });
            }
            return description;
            """,
            content_div,
        )
    except Exception:
        pass

    return (
        "<p>"
        + manufacturer_block
        + "</p><p>"
        + model_block
        + "</p><p>"
        + barcode_block
        + "</p><br><h3>O proizvodu</h3>"
        + transformed_html
    )


def scrape_product_details(
    driver: webdriver.Firefox,
    link: str,
    category_raw: str,
    skip_log: Callable[[str], None] | None = None,
) -> dict | None:
    driver.get(link)
    time.sleep(0.35)
    qty = extract_stock_qty(driver)
    if qty is not None and qty <= 0:
        if skip_log:
            skip_log(f"[skip] detail_stock_zero | category={category_raw} | link={link} | qty={qty}")
        return None
    title = clean_zero_like(
        safe_text(
        driver,
        "body > div.wrapper > section > div.main > div > div > div.col-md-6.col-sm-12.product-flex-info > div > h1",
        )
    )
    if not title:
        if skip_log:
            skip_log(f"[skip] detail_no_title | category={category_raw or ''} | link={link}")
        return None
    manufacturer = extract_manufacturer(driver)
    images = clean_zero_like(extract_images(driver))
    description = clean_zero_like(extract_description(driver))

    price_raw = extract_price_text_detail(driver)
    price = parse_price(price_raw)

    if not is_positive_price(price):
        if skip_log:
            skip_log(
             f"[skip] detail_bad_price | category={category_raw or ''} | link={link} | raw_price={price_raw or ''} | parsed_price={price or ''}"
            )
        return None


    row = {
        "Kategorija": promjenaKategorije(category_raw or ""),
        "Proizvodjac": manufacturer,
        "Naslov": title,
        "Opis": description,
        "Cijena": price,
        "Slike": images,
        "Link": link,
    }
    zero_columns = [column for column in OUTPUT_COLUMNS if column != "Link" and is_zero_like(row.get(column))]
    if zero_columns:
        if skip_log:
            skip_log(
                f"[skip] detail_zero_value | category={category_raw or ''} | link={link} | columns={','.join(zero_columns)}"
            )
        return None
    return row


def main(save_directory: str, geckodriver_path: str | None = None, headless: bool = False) -> str:
    os.makedirs(save_directory, exist_ok=True)
    categories_dir = os.path.join(save_directory, "kategorije")
    os.makedirs(categories_dir, exist_ok=True)
    skip_log_path = os.path.join(save_directory, "gama_skip.log")

    driver = build_driver(geckodriver_path=geckodriver_path, headless=headless)
    out_path = os.path.join(save_directory, "gama_all.xlsx")

    all_rows = []
    seen_global_links = set()
    used_category_files = set()

    def log_skip(message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line)
        try:
            with open(skip_log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:
            pass

    try:
        log_skip("Starting skip logging")
        login(driver, USERNAME, PASSWORD)
        category_links = discover_categories(driver)
        print(f"Discovered deep subcategories: {len(category_links)}")

        if not category_links:
            log_skip("[skip] no_categories_discovered")
            raise RuntimeError(
                "No deep subcategories were discovered. "
                "Rule in effect: only depth >= 3 under /proizvodi is parsed."
            )

        for category_link in category_links:
            category_rows = []
            category_name = ""
            try:
                print(f"Parsing category: {category_link}")
                driver.get(category_link)

                category_name = current_category_text(driver)
                if not category_name:
                    category_name = category_link.rstrip("/").split("/")[-1].replace("-", " ").strip() or "KATEGORIJA"

                final_count = scroll_until_products_stop(driver)
                print(f"[scroll] Loaded {final_count} product cards for '{category_name}'")

                products = get_listing_products(driver, skip_log=log_skip, category_name=category_name)
                print(f"Found {len(products)} listing products in '{category_name}'")

                seen_category_links = set()
                for product in products:
                    link = str(product.get("link") or "").strip()
                    if not link or link in seen_category_links:
                        if link and link in seen_category_links:
                            log_skip(
                                f"[skip] duplicate_link_in_category | category={category_name} | link={link}"
                            )
                        continue
                    seen_category_links.add(link)

                    row = scrape_product_details(driver, link, category_name, skip_log=log_skip)
                    if row is None:
                        log_skip(f"[skip] detail_row_none | category={category_name} | link={link}")
                        continue

                    category_rows.append(row)
                    if link in seen_global_links:
                        log_skip(f"[skip] duplicate_link_global | category={category_name} | link={link}")
                        continue
                    seen_global_links.add(link)
                    all_rows.append(row)
            except Exception as category_exc:
                if not category_name:
                    category_name = category_link.rstrip("/").split("/")[-1].replace("-", " ").strip() or "KATEGORIJA"
                print(f"Category parse failed: {category_link} -> {category_exc}")
                log_skip(f"[skip] category_parse_failed | category_link={category_link} | error={category_exc}")

            df_category = pd.DataFrame(category_rows, columns=OUTPUT_COLUMNS)
            base_name = sanitize_filename(category_name.upper())
            category_file = f"{base_name}.xlsx"
            if category_file in used_category_files:
                suffix = hashlib.md5(category_link.encode("utf-8")).hexdigest()[:8]
                category_file = f"{base_name}-{suffix}.xlsx"
            used_category_files.add(category_file)

            category_path = os.path.join(categories_dir, category_file)
            df_category.to_excel(category_path, index=False)
            print(f"Saved category file: {category_path} ({len(df_category)} rows)")

        df_all = pd.DataFrame(all_rows, columns=OUTPUT_COLUMNS)
        df_all.to_excel(out_path, index=False)
        print(f"Saved ALL file: {out_path} ({len(df_all)} rows)")
        return out_path
    finally:
        driver.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Gama Selenium scraper: parse only deep subcategories (depth >= 3)."
    )
    parser.add_argument(
        "save_directory",
        nargs="?",
        default=os.getcwd(),
        help="Folder for output Excel files (default: current working directory).",
    )
    parser.add_argument(
        "--geckodriver",
        default=None,
        help="Path to geckodriver.exe (default auto-detection near script/exe).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Firefox in headless mode (default is headed).",
    )
    args = parser.parse_args()

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{started}] Starting Gama subcategory scrape")
    output = main(
        save_directory=args.save_directory,
        geckodriver_path=args.geckodriver,
        headless=args.headless,
    )
    print(f"Scrape completed: {output}")
