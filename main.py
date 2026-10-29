import os
import json
import html
import secrets
import asyncio
from urllib.parse import urlencode

import aiohttp
import asyncpg
import discord
from discord import app_commands
from discord.ext import commands

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
import uvicorn


# ==========================================
# 1. 환경변수 설정
# ==========================================

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
SESSION_SECRET = os.environ["SESSION_SECRET"]
BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")

REDIRECT_URI = f"{BASE_URL}/auth/callback"


# ==========================================
# 2. 디스코드 봇 설정
# ==========================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

app = FastAPI(title="Discord Vote Website")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=True,
    same_site="lax",
    max_age=604800
)

pool = None
bot_task = None


# ==========================================
# 3. 데이터베이스
# ==========================================

async def init_database():
    global pool

    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=15
    )

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id BIGSERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                creator_id TEXT NOT NULL,
                options JSONB NOT NULL,
                created_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ballots (
                id BIGSERIAL PRIMARY KEY,
                vote_id BIGINT NOT NULL
                    REFERENCES votes(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                option_index INTEGER NOT NULL,
                created_at TIMESTAMPTZ
                    NOT NULL DEFAULT NOW(),

                UNIQUE(vote_id, user_id)
            )
        """)

    print("데이터베이스 연결 완료")


# ==========================================
# 4. 디스코드 서버 멤버 확인
# ==========================================

async def check_member(user_id: str) -> bool:
    url = (
        "https://discord.com/api/v10/"
        f"guilds/{GUILD_ID}/members/{user_id}"
    )

    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}"
    }

    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        async with session.get(
            url,
            headers=headers
        ) as response:

            if response.status == 200:
                return True

            if response.status == 404:
                return False

            if response.status in (401, 403):
                raise HTTPException(
                    503,
                    "봇의 서버 멤버 확인 권한을 점검해 주세요."
                )

            raise HTTPException(
                503,
                "디스코드 서버 확인에 실패했습니다."
            )


# ==========================================
# 5. 디스코드 OAuth2 로그인
# ==========================================

async def exchange_code(code: str):
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(
        timeout=timeout
    ) as session:

        # 로그인 코드를 액세스 토큰으로 교환
        async with session.post(
            "https://discord.com/api/v10/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={
                "Content-Type":
                    "application/x-www-form-urlencoded"
            }
        ) as response:

            if response.status != 200:
                raise HTTPException(
                    400,
                    "로그인 토큰 교환에 실패했습니다."
                )

            token_data = await response.json()

        # 인증된 디스코드 계정 정보 가져오기
        async with session.get(
            "https://discord.com/api/v10/users/@me",
            headers={
                "Authorization":
                    f"Bearer {token_data['access_token']}"
            }
        ) as response:

            if response.status != 200:
                raise HTTPException(
                    400,
                    "디스코드 계정을 확인하지 못했습니다."
                )

            return await response.json()


@app.get("/login")
async def login(request: Request, next: str = "/"):
    # 외부 사이트로 리다이렉트되는 것을 방지
    if not next.startswith("/") or next.startswith("//"):
        next = "/"

    state = secrets.token_urlsafe(32)

    request.session["oauth_state"] = state
    request.session["after_login"] = next

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
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
    code: str = "",
    state: str = "",
    error: str = ""
):
    if error:
        raise HTTPException(
            400,
            "디스코드 로그인이 취소되었습니다."
        )

    expected_state = request.session.get("oauth_state")

    if (
        not code
        or not state
        or not expected_state
        or not secrets.compare_digest(
            state, expected_state
        )
    ):
        raise HTTPException(
            400,
            "로그인 보안 확인에 실패했습니다. 다시 로그인해 주세요."
        )

    next_url = request.session.get(
        "after_login", "/"
    )

    request.session.pop("oauth_state", None)
    request.session.pop("after_login", None)

    user = await exchange_code(code)

    # 서버 측에서 실제 멤버인지 확인
    if not await check_member(str(user["id"])):
        request.session.clear()

        return HTMLResponse(
            page(
                "참여할 수 없습니다",
                """
                <p>이 투표는 지정된 디스코드 서버의
                멤버만 이용할 수 있습니다.</p>
                <a class="button" href="/">홈으로</a>
                """
            ),
            status_code=403
        )

    # 사용자 ID는 Discord API에서 확인한 값만 저장
    request.session.clear()

    request.session["user"] = {
        "id": str(user["id"]),
        "username": (
            user.get("global_name")
            or user.get("username")
            or "Discord 사용자"
        )
    }

    return RedirectResponse(
        next_url, status_code=303
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()

    return RedirectResponse("/", status_code=303)


# ==========================================
# 6. 웹사이트 디자인
# ==========================================

STYLE = """
<style>
* { box-sizing: border-box; }

body {
    margin: 0;
    background: #0b1020;
    color: #f0f3ff;
    font-family: Arial, sans-serif;
}

.wrap {
    max-width: 700px;
    margin: 45px auto;
    padding: 16px;
}

.card {
    background: #151c30;
    border: 1px solid #303b58;
    border-radius: 20px;
    padding: 26px;
}

h1 { font-size: 29px; }

a { color: #b6c8ff; }

.button {
    display: inline-block;
    background: #5865f2;
    color: white;
    border: none;
    border-radius: 10px;
    padding: 13px 18px;
    text-decoration: none;
    cursor: pointer;
    font-size: 15px;
    font-weight: bold;
}

.option {
    display: flex;
    align-items: center;
    gap: 12px;
    border: 1px solid #3b4765;
    border-radius: 12px;
    padding: 14px;
    margin: 10px 0;
    cursor: pointer;
}

.option input {
    width: 18px;
    height: 18px;
    accent-color: #7885ff;
}

.result { margin: 18px 0; }

.result-head {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 7px;
}

.bar {
    background: #303b58;
    height: 10px;
    border-radius: 20px;
    overflow: hidden;
}

.bar span {
    display: block;
    background: #7885ff;
    height: 100%;
}

.muted { color: #aab5d1; font-size: 13px; }

.success {
    background: #123a32;
    padding: 14px;
    border-radius: 10px;
}

hr {
    border: 0;
    border-top: 1px solid #303b58;
    margin: 25px 0;
}
</style>
"""


def page(title: str, content: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport"
              content="width=device-width, initial-scale=1">
        <title>{html.escape(title)}</title>
        {STYLE}
    </head>
    <body>
        <main class="wrap">
            <section class="card">
                {content}
            </section>
        </main>
    </body>
    </html>
    """


# ==========================================
# 7. 홈페이지
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = request.session.get("user")

    if user:
        account = (
            f"<p>로그인: "
            f"<b>{html.escape(user['username'])}</b></p>"
            '<a href="/logout">로그아웃</a>'
        )
    else:
        account = (
            '<a class="button" href="/login">'
            'Discord로 로그인</a>'
        )

    return HTMLResponse(page(
        "디스코드 투표",
        f"""
        <h1>🗳️ 디스코드 투표</h1>
        <p>디스코드 서버 멤버 전용 투표 사이트입니다.</p>
        {account}
        <hr>
        <p class="muted">
            디스코드에서 전달받은 투표 링크를 열어 주세요.
        </p>
        """
    ))


# ==========================================
# 8. 디스코드 봇의 /투표 명령어
# ==========================================

@bot.event
async def on_ready():
    try:
        guild = discord.Object(id=GUILD_ID)

        bot.tree.copy_global_to(guild=guild)

        synced = await bot.tree.sync(
            guild=guild
        )

        print(f"봇 로그인 완료: {bot.user}")
        print(f"슬래시 명령어 동기화: {len(synced)}개")

    except Exception as exc:
        print(f"명령어 동기화 오류: {exc}")


@bot.tree.command(
    name="투표",
    description="웹사이트 투표를 생성합니다."
)
@app_commands.describe(
    선택지="쉼표로 선택지를 구분하세요. 예: 사과, 바나나, 포도"
)
async def create_vote(
    interaction: discord.Interaction,
    선택지: str
):
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message(
            "이 서버에서는 투표를 만들 수 없습니다.",
            ephemeral=True
        )
        return

    options = [
        item.strip()
        for item in 선택지.split(",")
        if item.strip()
    ]

    # 중복 선택지 제거
    options = list(dict.fromkeys(options))

    if len(options) < 2:
        await interaction.response.send_message(
            "선택지를 2개 이상 입력하세요. "
            "예: 사과, 바나나, 포도",
            ephemeral=True
        )
        return

    if len(options) > 10:
        await interaction.response.send_message(
            "선택지는 최대 10개까지 가능합니다.",
            ephemeral=True
        )
        return

    if any(len(item) > 80 for item in options):
        await interaction.response.send_message(
            "각 선택지는 80자 이하여야 합니다.",
            ephemeral=True
        )
        return

    await interaction.response.defer()

    async with pool.acquire() as conn:
        vote_id = await conn.fetchval(
            """
            INSERT INTO votes (
                guild_id,
                creator_id,
                options
            )
            VALUES ($1, $2, $3::jsonb)
            RETURNING id
            """,
            str(GUILD_ID),
            str(interaction.user.id),
            json.dumps(options, ensure_ascii=False)
        )

    url = f"{BASE_URL}/vote/{vote_id}"

    embed = discord.Embed(
        title="🗳️ 새 투표가 생성되었습니다",
        description="\n".join(
            f"**{i + 1}.** {item}"
            for i, item in enumerate(options)
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="투표 참여",
        value=f"[웹사이트 열기]({url})",
        inline=False
    )

    embed.set_footer(
        text="Discord 로그인 필요 · 계정당 1회"
    )

    await interaction.followup.send(
        embed=embed
    )


# ==========================================
# 9. 투표 웹페이지
# ==========================================

@app.get("/vote/{vote_id}", response_class=HTMLResponse)
async def vote_page(
    vote_id: int,
    request: Request
):
    async with pool.acquire() as conn:
        vote = await conn.fetchrow(
            "SELECT * FROM votes WHERE id=$1",
            vote_id
        )

    if not vote:
        raise HTTPException(
            404, "투표를 찾을 수 없습니다."
        )

    user = request.session.get("user")

    if not user:
        return HTMLResponse(page(
            "로그인 필요",
            f"""
            <h1>🗳️ 투표 참여</h1>
            <p>투표하려면 디스코드 로그인이 필요합니다.</p>
            <a class="button"
               href="/login?next=/vote/{vote_id}">
               Discord로 로그인
            </a>
            """
        ))

    if not await check_member(user["id"]):
        request.session.clear()
        raise HTTPException(
            403, "지정된 서버의 멤버만 투표할 수 있습니다."
        )

    options = vote["options"]

    if isinstance(options, str):
        options = json.loads(options)

    async with pool.acquire() as conn:
        already_voted = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM ballots
                WHERE vote_id=$1 AND user_id=$2
            )
            """,
            vote_id,
            user["id"]
        )

    if already_voted:
        form_html = """
        <p class="success">
            이미 투표했습니다. 아래에서 실시간 결과를 확인하세요.
        </p>
        """
    else:
        choices = ""

        for index, option in enumerate(options):
            choices += f"""
            <label class="option">
                <input type="radio"
                       name="option_index"
                       value="{index}"
                       required>
                <span>{html.escape(option)}</span>
            </label>
            """

        form_html = f"""
        <form method="post"
              action="/vote/{vote_id}/cast">
            {choices}
            <button class="button" type="submit">
                투표하기
            </button>
        </form>
        """

    username = html.escape(user["username"])

    content = f"""
    <h1>🗳️ 투표</h1>
    <p>로그인: <b>{username}</b> ·
       <a href="/logout">로그아웃</a></p>

    {form_html}

    <hr>
    <h2>실시간 투표 결과</h2>
    <p id="total">총 0표</p>
    <div id="results">결과 불러오는 중...</div>

    <p class="muted">
        결과는 약 3초마다 자동으로 갱신됩니다.
    </p>

    <script>
    async function refreshResults() {{
        try {{
            const response = await fetch(
                "/api/vote/{vote_id}",
                {{ cache: "no-store" }}
            );

            if (!response.ok) {{
                throw new Error("결과를 불러오지 못했습니다.");
            }}

            const data = await response.json();

            document.getElementById("total").textContent =
                "총 " + data.total + "표";

            const results = document.getElementById("results");
            results.replaceChildren();

            for (const item of data.results) {{
                const row = document.createElement("div");
                row.className = "result";

                const head = document.createElement("div");
                head.className = "result-head";

                const name = document.createElement("span");
                name.textContent = item.option;

                const count = document.createElement("b");
                count.textContent =
                    item.count + "표 · " + item.percent + "%";

                head.append(name, count);

                const bar = document.createElement("div");
                bar.className = "bar";

                const fill = document.createElement("span");
                fill.style.width = item.percent + "%";

                bar.append(fill);
                row.append(head, bar);
                results.append(row);
            }}
        }} catch (error) {{
            document.getElementById("results").textContent =
                error.message;
        }}
    }}

    refreshResults();
    setInterval(refreshResults, 3000);
    </script>
    """

    return HTMLResponse(page("투표", content))


# ==========================================
# 10. 투표 제출
# ==========================================

@app.post("/vote/{vote_id}/cast")
async def cast_vote(
    vote_id: int,
    request: Request,
    option_index: int = Form(...)
):
    user = request.session.get("user")

    if not user:
        return RedirectResponse(
            f"/login?next=/vote/{vote_id}",
            status_code=303
        )

    if not await check_member(user["id"]):
        request.session.clear()
        raise HTTPException(
            403, "지정된 서버의 멤버만 투표할 수 있습니다."
        )

    async with pool.acquire() as conn:
        async with conn.transaction():

            vote = await conn.fetchrow(
                "SELECT options FROM votes WHERE id=$1",
                vote_id
            )

            if not vote:
                raise HTTPException(
                    404, "투표를 찾을 수 없습니다."
                )

            options = vote["options"]

            if isinstance(options, str):
                options = json.loads(options)

            if not 0 <= option_index < len(options):
                raise HTTPException(
                    400, "잘못된 선택지입니다."
                )

            try:
                await conn.execute(
                    """
                    INSERT INTO ballots (
                        vote_id,
                        user_id,
                        option_index
                    )
                    VALUES ($1, $2, $3)
                    """,
                    vote_id,
                    user["id"],
                    option_index
                )

            except asyncpg.UniqueViolationError:
                return HTMLResponse(
                    page(
                        "이미 투표함",
                        f"""
                        <h1>이미 투표했습니다.</h1>
                        <p>이 계정은 이 투표에 이미 참여했습니다.</p>
                        <a class="button"
                           href="/vote/{vote_id}">
                           결과 보기
                        </a>
                        """
                    ),
                    status_code=409
                )

    return RedirectResponse(
        f"/vote/{vote_id}",
        status_code=303
    )


# ==========================================
# 11. 실시간 투표 결과 API
# ==========================================

@app.get("/api/vote/{vote_id}")
async def vote_results(
    vote_id: int,
    request: Request
):
    user = request.session.get("user")

    if not user:
        raise HTTPException(
            401, "로그인이 필요합니다."
        )

    if not await check_member(user["id"]):
        raise HTTPException(
            403, "지정된 서버의 멤버만 결과를 볼 수 있습니다."
        )

    async with pool.acquire() as conn:
        vote = await conn.fetchrow(
            "SELECT options FROM votes WHERE id=$1",
            vote_id
        )

        if not vote:
            raise HTTPException(
                404, "투표를 찾을 수 없습니다."
            )

        rows = await conn.fetch(
            """
            SELECT option_index, COUNT(*) AS total
            FROM ballots
            WHERE vote_id=$1
            GROUP BY option_index
            """,
            vote_id
        )

    options = vote["options"]

    if isinstance(options, str):
        options = json.loads(options)

    counts = {
        row["option_index"]: row["total"]
        for row in rows
    }

    total = sum(counts.values())

    results = []

    for index, option in enumerate(options):
        count = counts.get(index, 0)

        percent = (
            round(count * 100 / total, 1)
            if total else 0
        )

        results.append({
            "option": option,
            "count": count,
            "percent": percent
        })

    return JSONResponse({
        "total": total,
        "results": results
    })


# ==========================================
# 12. 웹사이트와 봇 실행
# ==========================================

@app.on_event("startup")
async def startup():
    global bot_task

    await init_database()

    bot_task = asyncio.create_task(
        bot.start(DISCORD_TOKEN)
    )

    print("웹사이트와 디스코드 봇 실행 완료")


@app.on_event("shutdown")
async def shutdown():
    global bot_task

    if bot_task:
        bot_task.cancel()

    if not bot.is_closed():
        await bot.close()

    if pool:
        await pool.close()


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
