from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI(title="익명 투표")

KST = ZoneInfo("Asia/Seoul")
END_TIME = datetime(2026, 10, 31, 0, 0, 0, tzinfo=KST)

START_RESULTS = {
    "뽀야미": 2,
    "ist": 3,
    "한선우": 0,
}

FINAL_RESULTS = {
    "뽀야미": 2,
    "ist": 5,
    "한선우": 1,
}


def is_closed():
    return datetime.now(KST) >= END_TIME


def render_page():
    closed = is_closed()
    results = FINAL_RESULTS if closed else START_RESULTS
    total = sum(results.values())

    rows = ""
    for name, votes in results.items():
        percent = round(votes / total * 100) if total else 0
        rows += f"""
        <div class="option">
          <div class="line">
            <strong>{escape(name)}</strong>
            <span>{votes}표 · {percent}%</span>
          </div>
          <div class="track">
            <div class="bar" style="width:{percent}%"></div>
          </div>
        </div>
        """

    if closed:
        status = "투표 종료"
        timer = "투표가 종료되었습니다."
        winner = """
        <section class="winner">
          <p class="muted">최종 결과</p>
          <h2>🏆 ist</h2>
          <p>5표로 가장 많은 표를 획득했습니다.</p>
        </section>
        """
       
    else:
        status = "투표 진행 중"
        timer = f"종료 예정: {END_TIME.strftime('%Y년 %m월 %d일 %H:%M')} (한국 시간)"
        winner = ""
        notice = "현재 표는 실제 참가자의 투표 결과입니다."

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="1">
<title>익명 투표</title>
<style>
* {{ box-sizing: border-box; }}
body {{
  margin: 0; min-height: 100vh; padding: 20px;
  display: grid; place-items: center;
  background: #0b1020; color: #f3f5fb;
  font-family: Arial, sans-serif;
}}
.card {{
  width: 100%; max-width: 470px; padding: 26px;
  border-radius: 20px; background: #151d30;
  border: 1px solid #2c3853;
}}
.status {{
  display: inline-block; padding: 7px 12px;
  border-radius: 20px; background: #253653; color: #c8dcff;
}}
.muted {{ color: #aab5cc; font-size: 14px; }}
.option {{ margin: 22px 0; }}
.line {{ display: flex; justify-content: space-between; margin-bottom: 9px; }}
.track {{ height: 10px; border-radius: 10px; background: #29344b; overflow: hidden; }}
.bar {{ height: 100%; border-radius: 10px; background: #6495ff; }}
.winner {{ border-top: 1px solid #35415b; padding-top: 20px; margin-top: 25px; }}
.winner h2 {{ color: #91b5ff; }}
.notice {{ padding: 12px; border-radius: 10px; background: #202b40; color: #c6d0e2; font-size: 13px; line-height: 1.6; }}
</style>
</head>
<body>
<main class="card">
  <span class="status">{status}</span>
  <h1>익명 투표</h1>
  <p class="muted">누가 누구에게 투표했는지는 공개되지 않습니다.</p>
  <p>{timer}</p>
  {rows}
  {winner}
  <p class="notice">{notice}</p>
</main>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return render_page()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
