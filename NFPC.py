import difflib
import smtplib
import html
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import time
import os
import re

# ========= 환경변수(Secrets)에서 메일 설정 읽기 =========
MAIL_FROM = os.environ.get("MAIL_FROM", "tmddhks11@gmail.com")
MAIL_TO   = os.environ.get("MAIL_TO", "hosewa@lgensol.com")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))  # 587(STARTTLS) 권장
SMTP_USER = os.environ.get("SMTP_USER", MAIL_FROM)
SMTP_PASS = os.environ.get("SMTP_PASS", "")

def get_law_text(url):
    options = Options()
    # GitHub Actions/최신 크롬에서 headless 안정 옵션
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    # Actions에서 설치된 크롬 경로를 사용(로컬에서는 없어도 자동 건너뜀)
    chrome_path = os.environ.get("CHROME_PATH") or os.environ.get("GOOGLE_CHROME_BIN")
    if chrome_path:
        options.binary_location = chrome_path

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.get(url)
    time.sleep(3)
    driver.switch_to.frame("lawService")
    time.sleep(2)

    all_text = ""
    law_blocks = driver.find_elements(By.CSS_SELECTOR, "div.lawcon")
    for block in law_blocks:
        ps = block.find_elements(By.CSS_SELECTOR, "p")
        for p in ps:
            text = p.text.strip()
            if text:
                all_text += text + "\n"
        all_text += "\n"

    driver.quit()
    return all_text.strip()

def split_by_article(text):
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

def highlight_diff(a, b):
    matcher = difflib.SequenceMatcher(None, a, b)
    result_a = ""
    result_b = ""
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        chunk_a = html.escape(a[i1:i2])
        chunk_b = html.escape(b[j1:j2])
        if tag == 'equal':
            result_a += chunk_a
            result_b += chunk_b
        elif tag in ('replace', 'delete'):
            result_a += f"<b><span style='color:red'>{chunk_a}</span></b>"
        elif tag in ('replace', 'insert'):
            result_b += f"<b><span style='color:red'>{chunk_b}</span></b>"
    return result_a or "[없음]", result_b or "[없음]"

def get_changed_articles(new_text, old_text):
    new_map = split_by_article(new_text)
    old_map = split_by_article(old_text)

    changed = []
    for title, new_body in new_map.items():
        old_body = old_map.get(title, "")
        if new_body != old_body:
            before, after = highlight_diff(old_body, new_body)
            changed.append({
                "title": title,
                "before": before,
                "after": after
            })
    return changed

def send_email_notification(change_dict):
    sender_email = MAIL_FROM
    sender_password = SMTP_PASS
    receiver_email = MAIL_TO

    label_map = {
        "NFPC102": "옥내소화전 기준 (NFPC102)",
        "NFPC103": "스프링클러 기준 (NFPC103)",
        "NFPC109": "옥외소화전 기준 (NFPC109)"
    }

    changed_titles = []
    html_body = ""

    # 1) 본문(HTML) 구성
    for key, changes in change_dict.items():
        label = label_map[key]
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

    # 2) 제목
    if len(changed_titles) == 0:
        subject = "✅ NFPC 변경사항 자동 확인 - 변경 없음"
    elif len(changed_titles) >= 2:
        subject = "🚨 NFPC 변경 감지 (2개 이상)"
    else:
        subject = f"🚨 {changed_titles[0]} 변경 감지"

    # 3) 요약 라인 + 본문 합치기
    if changed_titles:
        summary_line = "변경된 기준: " + ", ".join(changed_titles)
    else:
        summary_line = "✅ 모든 기준(NFPC102, NFPC103, NFPC109)에 변경 사항 없음"

    html_final = f"<h1 style='color:red'>{summary_line}</h1><hr><br>" + html_body

    # 4) 메일 객체 생성
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg.attach(MIMEText(html_final, "html", "utf-8"))

    # 5) 전송 (465 SSL / 587 STARTTLS 모두 지원)
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

def save_combined_text(text_dict):
    combined = ""
    for key, text in text_dict.items():
        combined += f"### {key} ###\n{text.strip()}\n\n"
    with open("NFPC.txt", "w", encoding='utf-8') as f:
        f.write(combined.strip())

def load_combined_text():
    if not os.path.exists("NFPC.txt"):
        return {"NFPC102": "", "NFPC103": "", "NFPC109": ""}
    try:
        with open("NFPC.txt", "r", encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        with open("NFPC.txt", "r", encoding='cp949') as f:
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
        "NFPC109": "https://www.law.go.kr/행정규칙/옥외소화전설비의화재안전성능기준(NFPC109)"
    }

    new_texts = {}
    for key, url in urls.items():
        new_texts[key] = get_law_text(url)

    old_texts = load_combined_text()

    change_dict = {}
    for key in urls.keys():
        change_dict[key] = get_changed_articles(new_texts[key], old_texts.get(key, ""))

    send_email_notification(change_dict)
    save_combined_text(new_texts)

if __name__ == "__main__":
    main()