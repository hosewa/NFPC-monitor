# -*- coding: utf-8 -*-

import difflib
import smtplib
import html
import time
import os
import re

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


# ========= 메일 설정: GitHub Secrets/환경변수에서 읽기 =========
MAIL_FROM = os.environ.get("MAIL_FROM", "tmddhks11@gmail.com")
MAIL_TO   = os.environ.get("MAIL_TO", "hosewa@lgensol.com")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")

# ✅ SMTP_PORT 방어 로직 (비어있거나 숫자 아니면 자동 587)
_port = (os.environ.get("SMTP_PORT") or "587").strip()
try:
    SMTP_PORT = int(_port)
except ValueError:
    SMTP_PORT = 587

SMTP_USER = os.environ.get("SMTP_USER", MAIL_FROM)
SMTP_PASS = os.environ.get("SMTP_PASS", "")


def get_law_text(url: str) -> str:
    """법령 본문 텍스트 수집 (iframe 전환 + 안정 대기 + 실패시 스냅샷 저장)"""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ko-KR")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    # GitHub Actions에서 설치된 크롬 경로 사용(로컬에서는 없으면 무시)
    chrome_path = os.environ.get("CHROME_PATH") or os.environ.get("GOOGLE_CHROME_BIN")
    if chrome_path:
        options.binary_location = chrome_path

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.get(url)

    try:
        wait = WebDriverWait(driver, 40)  # 충분히 대기

        # iframe 준비될 때까지 기다린 뒤 전환 (id → name 순서로 재시도)
        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it((By.ID, "lawService")))
        except Exception:
            try:
                wait.until(EC.frame_to_be_available_and_switch_to_it((By.NAME, "lawService")))
            except Exception:
                # 어떤 iframe이 있는지 로그
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                print("[DEBUG] iframe count:", len(frames))
                for i, f in enumerate(frames):
                    fid = f.get_attribute("id")
                    fname = f.get_attribute("name")
                    src = f.get_attribute("src")
                    print(f"[DEBUG] iframe[{i}] id={fid} name={fname} src={src}")
                raise

        # 본문 요소 로딩 대기
        wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "div.lawcon p")))
        time.sleep(1)

        all_text = ""
        for block in driver.find_elements(By.CSS_SELECTOR, "div.lawcon"):
            for p in block.find_elements(By.CSS_SELECTOR, "p"):
                t = p.text.strip()
                if t:
                    all_text += t + "\n"
            all_text += "\n"

        return all_text.strip()

    except Exception as e:
        # 실패 시 스냅샷 남기기 (디버깅용)
        try:
            fname = (url.split("/")[-1] or "page")[:30]
            with open(f"debug_{fname}.html", "w", encoding="utf-8") as f:
                f.write(driver.page_source)
            driver.save_screenshot(f"debug_{fname}.png")
            print(f"[DEBUG] saved page_source/screenshot for {fname}")
        except Exception as ee:
            print("[DEBUG] snapshot failed:", ee)
        raise
    finally:
        driver.quit()


def split_by_article(text: str) -> dict:
    """'제n조' 기준으로 조문 분리"""
    article_map = {}
    current_title = None
    current_body = []

    for line in text.splitlines():
        if re.match(r"^제\d+조", line):
            if current_title:
                article_map[current_title] = "\n".join(current_body).strip()
            current_title = line.strip()
            current_body = []
        else:
            current_body.append(line.strip())

    if current_title:
        article_map[current_title] = "\n".join(current_body).strip()
    return article_map


def highlight_diff(a: str, b: str) -> tuple[str, str]:
    """두 텍스트 차이를 빨간색 <b>로 하이라이트"""
    matcher = difflib.SequenceMatcher(None, a, b)
    result_a, result_b = "", ""

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        chunk_a = html.escape(a[i1:i2])
        chunk_b = html.escape(b[j1:j2])
        if tag == "equal":
            result_a += chunk_a
            result_b += chunk_b
        elif tag in ("replace", "delete"):
            result_a += f"<b><span style='color:red'>{chunk_a}</span></b>"
        elif tag in ("replace", "insert"):
            result_b += f"<b><span style='color:red'>{chunk_b}</span></b>"

    return result_a or "[없음]", result_b or "[없음]"


def get_changed_articles(new_text: str, old_text: str) -> list[dict]:
    """조문 단위로 변경된 항목 목록"""
    new_map = split_by_article(new_text)
    old_map = split_by_article(old_text)

    changed = []
    for title, new_body in new_map.items():
        old_body = old_map.get(title, "")
        if new_body != old_body:
            before, after = highlight_diff(old_body, new_body)
            changed.append({"title": title, "before": before, "after": after})
    return changed


def send_email_notification(change_dict: dict, errors: dict | None = None) -> None:
    """변경 사항/오류 요약을 HTML 메일로 송부"""
    sender_email = MAIL_FROM
    sender_password = SMTP_PASS
    receiver_email = MAIL_TO

    label_map = {
        "NFPC102": "옥내소화전 기준 (NFPC102)",
        "NFPC103": "스프링클러 기준 (NFPC103)",
        "NFPC109": "옥외소화전 기준 (NFPC109)",
    }

    changed_titles = []
    html_body = ""

    # 오류가 있으면 상단에 상태 표기
    if errors:
        html_body += "<h2>⚠️ 수집 중 오류가 발생한 항목</h2><ul>"
        for k, msg in errors.items():
            html_body += f"<li><b>{k}</b>: {html.escape(str(msg))}</li>"
        html_body += "</ul><hr>"

    # 본문 구성
    for key, changes in change_dict.items():
        label = label_map.get(key, key)
        if changes:
            changed_titles.append(label)
            html_body += f"<h2>🚨 {label} 변경 감지 ({len(changes)}개 조문)</h2><br>"
            for ch in changes:
                html_body += f"<h3>🔸 {ch['title']}</h3>"
                html_body += f"<p><b>[변경 전]</b><br>{ch['before'].replace(chr(10), '<br>')}</p>"
                html_body += f"<p><b>[변경 후]</b><br>{ch['after'].replace(chr(10), '<br>')}</p>"
                html_body += "<hr style='border-top:1px dashed #999;'>"
        else:
            html_body += f"<h2>✅ {label} 변경 없음</h2><br>"

    # 제목/요약
    if len(changed_titles) == 0:
        subject = "✅ NFPC 변경사항 자동 확인 - 변경 없음"
        summary_line = "✅ 모든 기준(NFPC102, NFPC103, NFPC109)에 변경 사항 없음"
    elif len(changed_titles) >= 2:
        subject = "🚨 NFPC 변경 감지 (2개 이상)"
        summary_line = "변경된 기준: " + ", ".join(changed_titles)
    else:
        subject = f"🚨 {changed_titles[0]} 변경 감지"
        summary_line = "변경된 기준: " + ", ".join(changed_titles)

    html_final = f"<h1 style='color:red'>{summary_line}</h1><hr><br>" + html_body

    # 메일 전송
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg.attach(MIMEText(html_final, "html", "utf-8"))

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, 465) as server:
                server.login(sender_email, sender_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(sender_email, sender_password)
                server.send_message(msg)
        print("📧 이메일 전송 완료")
    except Exception as e:
        print("❌ 이메일 전송 실패:", e)


def save_combined_text(text_dict: dict) -> None:
    """수집한 원문을 합쳐 파일로 저장 (다음 비교용)"""
    combined = ""
    for key, text in text_dict.items():
        combined += f"### {key} ###\n{text.strip()}\n\n"
    with open("NFPC.txt", "w", encoding="utf-8") as f:
        f.write(combined.strip())


def load_combined_text() -> dict:
    """이전 수집본 로드(없으면 빈 값)"""
    if not os.path.exists("NFPC.txt"):
        return {"NFPC102": "", "NFPC103": "", "NFPC109": ""}

    try:
        with open("NFPC.txt", "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        with open("NFPC.txt", "r", encoding="cp949") as f:
            content = f.read()

    result = {"NFPC102": "", "NFPC103": "", "NFPC109": ""}
    current_key = None
    for line in content.splitlines():
        if "### NFPC102 ###" in line:
            current_key = "NFPC102"
            continue
        elif "### NFPC103 ###" in line:
            current_key = "NFPC103"
            continue
        elif "### NFPC109 ###" in line:
            current_key = "NFPC109"
            continue
        elif current_key:
            result[current_key] += line + "\n"

    return result


def main():
    print("🕒 NFPC 기준 점검 시작")

    urls = {
        "NFPC102": "https://www.law.go.kr/행정규칙/옥내소화전설비의화재안전성능기준(NFPC102)",
        "NFPC103": "https://www.law.go.kr/행정규칙/스프링클러설비의화재안전성능기준(NFPC103)",
        "NFPC109": "https://www.law.go.kr/행정규칙/옥외소화전설비의화재안전성능기준(NFPC109)",
    }

    new_texts = {}
    errors = {}

    # 수집 단계 (오류가 나도 전체 중단하지 않고 이어감)
    for key, url in urls.items():
        try:
            new_texts[key] = get_law_text(url)
        except Exception as e:
            errors[key] = f"{type(e).__name__}: {e}"
            new_texts[key] = ""

    old_texts = load_combined_text()

    # 변경 비교
    change_dict = {}
    for key in urls.keys():
        try:
            change_dict[key] = get_changed_articles(new_texts[key], old_texts.get(key, ""))
        except Exception as e:
            errors[key] = f"{type(e).__name__}: {e}"
            change_dict[key] = []

    # 메일 전송 + 스냅샷 저장
    send_email_notification(change_dict, errors)
    save_combined_text(new_texts)


if __name__ == "__main__":
    main()
