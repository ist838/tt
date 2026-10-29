
import os
import json
import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from html import escape

import aiohttp
import asyncpg
import discord
from discord import app_commands
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
import uvicorn


# ==================================================
# 환경 변수
# ==================================================

def env_required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Render 환경 변수 {name}을(를) 설정해야 합니다."
        )
    return value


DISCORD_TOKEN = env_required("DISCORD_TOKEN")
DISCORD_CLIENT_ID = env_required("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = env_required("DISCORD_CLIENT_SECRET")
DISCORD_GUILD_ID = env_required("DISCORD_GUILD_ID")
DATABASE_URL = env_required("DATABASE_URL")
SESSION_SECRET = env_required("SESSION_SECRET")
PUBLIC_BASE_URL = env_required("PUBLIC_BASE_URL").rstrip("/")

OAUTH_REDIRECT_URI = PUBLIC_BASE_URL + "/auth/callback"
VOTE_DURATION = timedelta(hours=12)

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

app = FastAPI(title="풍뢰부대 투표")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=True,
    max_age=604800,
)

pool = None
bot_task = None


# ==================================================
# 데이터베이스
# ==================================================

async def init_db():
    global pool

    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id BIGSERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                question TEXT NOT NULL DEFAULT '풍뢰부대 투표',
                options JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)

        await conn.execute("""
            ALTER TABLE votes
            ADD COLUMN IF NOT EXISTS question TEXT
            NOT NULL DEFAULT '풍뢰부대 투표'
        """)

        await conn.execute("""
            ALTER TABLE votes
            ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ
        """)

        # 이전에 생성한 투표도 마감 시간을 갖도록 처리
        await conn.execute("""
            UPDATE votes
            SET expires_at =
                COALESCE(created_at, NOW()) + INTERVAL '12 hours'
            WHERE expires_at IS NULL
        """)

        await conn.execute("""
            ALTER TABLE votes
            ALTER COLUMN expires_at SET NOT NULL
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ballots (
                id BIGSERIAL PRIMARY KEY,
                vote_id BIGINT NOT NULL
                    REFERENCES votes(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                option_index INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (vote_id, user_id)
            )
        """)


def db():
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="데이터베이스가 준비되지 않았습니다."
        )
    return pool


def decode_options(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


# ==================================================
# 디스코드 서버 멤버 확인
# ==================================================

async def check_guild_member(user_id):
    url = (
        f"https://discord.com/api/v10/guilds/"
        f"{DISCORD_GUILD_ID}/members/{user_id}"
    )

    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}"
    }

    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                return True

            if response.status == 404:
                return False

            raise HTTPException(
                status_code=503,
                detail=(
                    "디스코드 멤버 확인에 실패했습니다. "
                    "봇 초대 및 서버 ID 설정을 확인하세요."
                )
            )


async def require_member(request):
    user = request.session.get("user")

    if not user:
        raise HTTPException(
            status_code=401,
            detail="먼저 디스코드 로그인을 해주세요."
        )

    if not await check_guild_member(user["id"]):
        request.session.clear()
        raise HTTPException(
            status_code=403,
            detail="풍뢰부대 서버 멤버만 이용할 수 있습니다."
        )

    return user


# ==================================================
# 디스코드 로그인
# ==================================================

@app.get("/login")
async def login(request: Request, next: str = "/"):
    if not next.startswith("/") or next.startswith("//"):
        next = "/"

    state = secrets.token_urlsafe(32)

    request.session["oauth_state"] = state
    request.session["after_login"] = next

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }

    url = (
        "https://discord.com/oauth2/authorize?"
        + urlencode(params)
    )

    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
):
    if error:
        return HTMLResponse(
            "디스코드 로그인이 취소되었습니다.",
            status_code=400,
        )

    saved_state = request.session.get("oauth_state")

    if (
        not code
        or not state
        or not saved_state
        or not secrets.compare_digest(state, saved_state)
    ):
        return HTMLResponse(
            "로그인 인증이 올바르지 않습니다. 다시 로그인하세요.",
            status_code=400,
        )

    async with aiohttp.ClientSession() as session:
        token_data = {
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OAUTH_REDIRECT_URI,
        }

        async with session.post(
            "https://discord.com/api/v10/oauth2/token",
            data=token_data,
        ) as response:
            if response.status != 200:
                return HTMLResponse(
                    "로그인 토큰을 가져오지 못했습니다. "
                    "Redirect URI를 확인하세요.",
                    status_code=502,
                )

            token = await response.json()

        async with session.get(
            "https://discord.com/api/v10/users/@me",
            headers={
                "Authorization":
                    f"Bearer {token['access_token']}"
            },
        ) as response:
            if response.status != 200:
                return HTMLResponse(
                    "디스코드 계정 정보를 가져오지 못했습니다.",
                    status_code=502,
                )

            profile = await response.json()

    if not await check_guild_member(str(profile["id"])):
        request.session.clear()
        return HTMLResponse(
            "<h2>풍뢰부대 서버 멤버만 투표할 수 있습니다.</h2>",
            status_code=403,
        )

    destination = request.session.get("after_login", "/")

    if not destination.startswith("/") or destination.startswith("//"):
        destination = "/"

    request.session.clear()

    request.session["user"] = {
        "id": str(profile["id"]),
        "username": (
            profile.get("global_name")
            or profile.get("username")
            or "Discord 사용자"
        ),
    }

    return RedirectResponse(destination, status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ==================================================
# /투표 명령어
# ==================================================

@tree.command(
    name="투표",
    description="12시간 동안 진행되는 투표를 만듭니다."
)
@app_commands.describe(
    선택지="쉼표로 구분 (예: 사과,바나나,포도)",
    질문="투표 제목",
)
async def create_vote(
    interaction: discord.Interaction,
    선택지: str,
    질문: str = "풍뢰부대 투표",
):
    if (
        interaction.guild_id is None
        or str(interaction.guild_id) != DISCORD_GUILD_ID
    ):
        await interaction.response.send_message(
            "설정된 풍뢰부대 서버에서만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    options = [
        item.strip()
        for item in 선택지.split(",")
        if item.strip()
    ]

    if len(options) < 2:
        await interaction.response.send_message(
            "선택지를 2개 이상 입력해 주세요.",
            ephemeral=True,
        )
        return

    if len(options) > 10:
        await interaction.response.send_message(
            "선택지는 최대 10개입니다.",
            ephemeral=True,
        )
        return

    if len(set(x.casefold() for x in options)) != len(options):
        await interaction.response.send_message(
            "중복된 선택지는 사용할 수 없습니다.",
            ephemeral=True,
        )
        return

    if any(len(x) > 80 for x in options):
        await interaction.response.send_message(
            "각 선택지는 80자 이내로 입력해 주세요.",
            ephemeral=True,
        )
        return

    question = (질문 or "풍뢰부대 투표").strip()[:180]

    if not question:
        question = "풍뢰부대 투표"

    now = datetime.now(timezone.utc)
    expires_at = now + VOTE_DURATION

    try:
        async with db().acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO votes (
                    guild_id, creator_id, question,
                    options, created_at, expires_at
                )
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                RETURNING id
                """,
                str(interaction.guild_id),
                str(interaction.user.id),
                question,
                json.dumps(options, ensure_ascii=False),
                now,
                expires_at,
            )
    except Exception as exc:
        print("투표 생성 오류:", repr(exc))
        await interaction.response.send_message(
            "투표 생성에 실패했습니다. Render 로그를 확인하세요.",
            ephemeral=True,
        )
        return

    vote_url = f"{PUBLIC_BASE_URL}/vote/{row['id']}"

    embed = discord.Embed(
        title=f"📊 {question}",
        description=(
            f"선택지: {' · '.join(options)}\n\n"
            f"마감: <t:{int(expires_at.timestamp())}:F>\n"
            f"남은 시간: <t:{int(expires_at.timestamp())}:R>\n\n"
            f"[투표 페이지 열기]({vote_url})"
        ),
        color=discord.Color.blue(),
        timestamp=now,
    )

    embed.set_footer(text="풍뢰부대 투표 | 계정당 1회")

    await interaction.response.send_message(embed=embed)


@bot.event
async def on_ready():
    try:
        guild = discord.Object(id=int(DISCORD_GUILD_ID))
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)

        print(
            f"디스코드 로그인 성공: {bot.user} / "
            f"명령어 {len(synced)}개 동기화"
        )
    except Exception as exc:
        print("명령어 동기화 오류:", repr(exc))


# ==================================================
# 웹사이트 공통 디자인
# ==================================================

def page(title, content):
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} | 풍뢰부대</title>
<style>
*{{box-sizing:border-box}}
body{{
 margin:0;background:#0b1020;color:#eef2ff;
 font-family:system-ui,sans-serif
}}
main{{max-width:760px;margin:40px auto;padding:16px}}
.card{{
 background:#141c30;border:1px solid #293653;
 border-radius:18px;padding:26px;
 box-shadow:0 15px 45px #0004
}}
h1{{overflow-wrap:anywhere}}
p{{line-height:1.7}}
.muted{{color:#aab6d2;font-size:14px}}
.btn{{
 display:inline-block;background:#477dff;color:white;
 padding:12px 16px;border:0;border-radius:10px;
 text-decoration:none;font-weight:bold;cursor:pointer;
 margin:8px 5px 8px 0
}}
.btn:disabled{{opacity:.5;cursor:not-allowed}}
.secondary{{background:#293653}}
.pill{{
 background:#253657;border:1px solid #3b507a;
 padding:6px 10px;border-radius:30px;font-size:12px
}}
.topline{{display:flex;justify-content:space-between;gap:8px}}
.options{{display:grid;gap:12px;margin:24px 0}}
.option{{
 padding:14px;border:1px solid #2d3d5e;
 border-radius:13px;background:#10182a
}}
.option-label{{display:flex;gap:10px;align-items:center}}
.option-label input{{width:18px;height:18px}}
.stats{{font-size:13px;margin-top:12px;color:#c8d3eb}}
.bar{{height:8px;background:#283653;border-radius:20px;margin-top:7px}}
.fill{{height:100%;background:#6590ff;border-radius:20px}}
.message{{color:#bcd0ff;min-height:1.5em}}
</style>
</head>
<body><main>{content}</main></body>
</html>"""


# ==================================================
# 메인 페이지
# ==================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = request.session.get("user")

    if user:
        auth = (
            f"<p>로그인: <b>{escape(user['username'])}</b></p>"
            '<a class="btn secondary" href="/logout">로그아웃</a>'
        )
    else:
        auth = '<a class="btn" href="/login">디스코드 로그인</a>'

    return HTMLResponse(page(
        "풍뢰부대 투표",
        f"""
        <section class="card">
          <h1>📊 풍뢰부대 투표</h1>
          <p>디스코드에서 생성된 투표에 참여하세요.</p>
          {auth}
          <p class="muted">
            투표는 생성 후 12시간 동안 진행됩니다.
            계정당 한 번만 투표할 수 있습니다.
          </p>
        </section>
        """
    ))


# ==================================================
# 투표 페이지
# ==================================================

@app.get("/vote/{vote_id}", response_class=HTMLResponse)
async def vote_page(request: Request, vote_id: int):
    async with db().acquire() as conn:
        vote = await conn.fetchrow(
            "SELECT * FROM votes WHERE id=$1",
            vote_id,
        )

    if not vote:
        raise HTTPException(status_code=404, detail="투표가 없습니다.")

    user = request.session.get("user")

    if not user:
        login_url = f"/login?next=/vote/{vote_id}"

        return HTMLResponse(page(
            vote["question"],
            f"""
            <section class="card">
              <h1>{escape(vote['question'])}</h1>
              <p>풍뢰부대 디스코드 서버 멤버만 참여할 수 있습니다.</p>
              <a class="btn" href="{login_url}">
                디스코드 로그인 후 투표하기
              </a>
            </section>
            """
        ))

    try:
        await require_member(request)
    except HTTPException as exc:
        if exc.status_code == 403:
            return HTMLResponse(
                page(
                    "접근 불가",
                    '<section class="card"><h1>풍뢰부대 서버 멤버만 이용할 수 있습니다.</h1></section>',
                ),
                status_code=403,
            )
        raise

    content = f"""
    <section class="card">
      <div class="topline">
        <span class="pill">풍뢰부대 투표</span>
        <span class="pill" id="status">불러오는 중...</span>
      </div>

      <h1>{escape(vote['question'])}</h1>
      <p id="deadline" class="muted"></p>
      <div id="options" class="options"></div>
      <p id="message" class="message"></p>
      <p id="total" class="muted"></p>
      <p class="muted">
        로그인: {escape(user['username'])} · 계정당 1회 투표
      </p>
      <a class="btn secondary" href="/">홈으로</a>
    </section>

    <script>
    const voteId = {vote_id};

    async function refreshVote() {{
      try {{
        const response = await fetch(
          `/api/vote/${{voteId}}`,
          {{cache:"no-store"}}
        );

        const data = await response.json();

        if (!response.ok) {{
          document.getElementById("message").textContent =
            data.detail || "결과를 불러오지 못했습니다.";
          return;
        }}

        const container = document.getElementById("options");
        const previous = document.querySelector(
          'input[name="choice"]:checked'
        )?.value;

        container.innerHTML = "";

        data.options.forEach((option, index) => {{
          const row = document.createElement("div");
          row.className = "option";

          const label = document.createElement("label");
          label.className = "option-label";

          const radio = document.createElement("input");
          radio.type = "radio";
          radio.name = "choice";
          radio.value = index;
          radio.disabled = data.closed || data.has_voted;

          if (previous === String(index)) {{
            radio.checked = true;
          }}

          const name = document.createElement("span");
          name.textContent = option;

          label.append(radio, name);

          const stats = document.createElement("div");
          stats.className = "stats";

          const count = document.createElement("div");
          count.textContent =
            `${{data.counts[index]}}표 · ${{data.percentages[index]}}%`;

          const bar = document.createElement("div");
          bar.className = "bar";

          const fill = document.createElement("div");
          fill.className = "fill";
          fill.style.width = `${{data.percentages[index]}}%`;

          bar.appendChild(fill);
          stats.append(count, bar);
          row.append(label, stats);
          container.appendChild(row);
        }});

        document.getElementById("status").textContent =
          data.closed ? "투표 마감" : "투표 진행 중";

        document.getElementById("deadline").textContent =
          "마감 시간: " + new Date(data.expires_at).toLocaleString();

        document.getElementById("total").textContent =
          `총 ${{data.total}}표`;

        const message = document.getElementById("message");

        if (data.closed) {{
          message.textContent =
            "투표가 마감되었습니다. 결과를 확인할 수 있습니다.";
        }} else if (data.has_voted) {{
          message.textContent =
            "이미 투표했습니다. 결과가 실시간으로 갱신됩니다.";
        }} else {{
          message.textContent = "";

          const button = document.createElement("button");
          button.className = "btn";
          button.textContent = "투표하기";
          button.onclick = castVote;
          container.appendChild(button);
        }}
      }} catch (error) {{
        document.getElementById("message").textContent =
          "연결 오류입니다. 잠시 후 다시 시도해 주세요.";
      }}
    }}

    async function castVote() {{
      const selected = document.querySelector(
        'input[name="choice"]:checked'
      );

      if (!selected) {{
        document.getElementById("message").textContent =
          "선택지를 먼저 골라 주세요.";
        return;
      }}

      const response = await fetch(
        `/vote/${{voteId}}/cast`,
        {{
          method:"POST",
          headers:{{"Content-Type":"application/json"}},
          body:JSON.stringify({{
            option_index:Number(selected.value)
          }})
        }}
      );

      const result = await response.json();

      document.getElementById("message").textContent =
        response.ok
          ? "투표가 반영되었습니다!"
          : (result.detail || "투표 실패");

      await refreshVote();
    }}

    refreshVote();
    setInterval(refreshVote, 3000);
    </script>
    """

    return HTMLResponse(page(vote["question"], content))


# ==================================================
# 실시간 결과 API
# ==================================================

@app.get("/api/vote/{vote_id}")
async def vote_results(request: Request, vote_id: int):
    user = await require_member(request)

    async with db().acquire() as conn:
        vote = await conn.fetchrow(
            "SELECT * FROM votes WHERE id=$1",
            vote_id,
        )

        if not vote:
            raise HTTPException(status_code=404, detail="투표가 없습니다.")

        rows = await conn.fetch(
            """
            SELECT option_index, COUNT(*)::int AS count
            FROM ballots
            WHERE vote_id=$1
            GROUP BY option_index
            """,
            vote_id,
        )

        existing = await conn.fetchval(
            """
            SELECT 1 FROM ballots
            WHERE vote_id=$1 AND user_id=$2
            """,
            vote_id,
            str(user["id"]),
        )

    options = decode_options(vote["options"])
    counts = [0] * len(options)

    for row in rows:
        index = row["option_index"]
        if 0 <= index < len(counts):
            counts[index] = row["count"]

    total = sum(counts)

    percentages = [
        round(count * 100 / total, 1) if total else 0
        for count in counts
    ]

    now = datetime.now(timezone.utc)
    closed = now >= vote["expires_at"]

    return JSONResponse({
        "id": vote_id,
        "question": vote["question"],
        "options": options,
        "counts": counts,
        "percentages": percentages,
        "total": total,
        "closed": closed,
        "expires_at": vote["expires_at"].isoformat(),
        "has_voted": bool(existing),
    })


# ==================================================
# 실제 투표 저장
# ==================================================

@app.post("/vote/{vote_id}/cast")
async def cast_vote(request: Request, vote_id: int):
    user = await require_member(request)

    try:
        payload = await request.json()
        option_index = int(payload.get("option_index"))
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="선택지를 올바르게 보내 주세요.",
        )

    async with db().acquire() as conn:
        async with conn.transaction():
            # 행 잠금으로 마감 시각과 투표 저장을 순서대로 처리
            vote = await conn.fetchrow(
                "SELECT * FROM votes WHERE id=$1 FOR UPDATE",
                vote_id,
            )

            if not vote:
                raise HTTPException(
                    status_code=404,
                    detail="투표를 찾을 수 없습니다.",
                )

            options = decode_options(vote["options"])

            if not 0 <= option_index < len(options):
                raise HTTPException(
                    status_code=400,
                    detail="선택지가 올바르지 않습니다.",
                )

            if datetime.now(timezone.utc) >= vote["expires_at"]:
                raise HTTPException(
                    status_code=400,
                    detail="투표가 마감되었습니다.",
                )

            result = await conn.execute(
                """
                INSERT INTO ballots (vote_id, user_id, option_index)
                VALUES ($1, $2, $3)
                ON CONFLICT (vote_id, user_id) DO NOTHING
                """,
                vote_id,
                str(user["id"]),
                option_index,
            )

            if result == "INSERT 0 0":
                raise HTTPException(
                    status_code=409,
                    detail="이미 투표에 참여했습니다.",
                )

    return JSONResponse({"ok": True})


# ==================================================
# 서버 시작
# ==================================================

@app.on_event("startup")
async def startup():
    global bot_task

    await init_db()

    bot_task = asyncio.create_task(
        bot.start(DISCORD_TOKEN)
    )

    print("풍뢰부대 투표 웹사이트 준비 완료")


@app.on_event("shutdown")
async def shutdown():
    global pool

    if bot_task and not bot_task.done():
        bot_task.cancel()

    if not bot.is_closed():
        await bot.close()

    if pool is not None:
        await pool.close()
        pool = None


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
