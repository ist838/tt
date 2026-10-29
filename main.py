
import os
import json
import secrets
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from html import escape

import asyncpg
import discord
import httpx
from discord import app_commands
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

load_dotenv()

# =========================
# 환경 변수
# =========================
TOKEN = os.getenv("DISCORD_TOKEN", "")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

KST = ZoneInfo("Asia/Seoul")
DEFAULT_END = datetime(2026, 10, 31, 0, 0, tzinfo=KST)
DISCORD_API = "https://discord.com/api/v10"

db = None
bot = discord.Client(intents=discord.Intents.none())
tree = app_commands.CommandTree(bot)


def now_utc():
    return datetime.now(timezone.utc)


def closed(end_at):
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    return now_utc() >= end_at.astimezone(timezone.utc)


def kst_time(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def get_base(request):
    return PUBLIC_URL or str(request.base_url).rstrip("/")


# =========================
# 데이터베이스
# =========================
async def init_db():
    global db

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 환경 변수가 없습니다.")

    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    ssl = "require" if "render.com" in url or "dpg-" in url else None
    db = await asyncpg.create_pool(
        url, min_size=1, max_size=5, ssl=ssl
    )

    async with db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                options JSONB NOT NULL,
                creator_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                end_at TIMESTAMPTZ NOT NULL
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ballots (
                id BIGSERIAL PRIMARY KEY,
                vote_id BIGINT NOT NULL
                    REFERENCES votes(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                option_index INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(vote_id, user_id)
            )
        """)


# =========================
# Discord 봇
# =========================
@bot.event
async def on_ready():
    print(f"Discord 로그인 성공: {bot.user}")

    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()

        print("슬래시 명령어 동기화 완료")
    except Exception as e:
        print("명령어 동기화 오류:", e)


@tree.command(name="투표", description="새 웹 투표를 만듭니다.")
@app_commands.describe(
    제목="투표 제목",
    선택지="쉼표로 구분 (예: 뽀야미, ist, 한선우)",
    마감일시="한국 시간 YYYY-MM-DD HH:MM"
)
async def create_vote(
    interaction: discord.Interaction,
    제목: app_commands.Range[str, 1, 100],
    선택지: app_commands.Range[str, 3, 1000],
    마감일시: str | None = None
):
    if db is None:
        await interaction.response.send_message(
            "데이터베이스가 준비되지 않았습니다.", ephemeral=True
        )
        return

    if not interaction.guild or str(interaction.guild.id) != GUILD_ID:
        await interaction.response.send_message(
            "설정된 서버에서만 사용할 수 있습니다.", ephemeral=True
        )
        return

    options = list(dict.fromkeys(
        x.strip() for x in 선택지.split(",") if x.strip()
    ))

    if len(options) < 2:
        await interaction.response.send_message(
            "선택지를 2개 이상 입력해 주세요.", ephemeral=True
        )
        return

    if len(options) > 20:
        await interaction.response.send_message(
            "선택지는 최대 20개입니다.", ephemeral=True
        )
        return

    try:
        if 마감일시:
            end_at = datetime.strptime(
                마감일시.strip(), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=KST).astimezone(timezone.utc)
        else:
            end_at = DEFAULT_END.astimezone(timezone.utc)
    except ValueError:
        await interaction.response.send_message(
            "마감일시 형식은 2026-10-31 00:00 입니다.",
            ephemeral=True
        )
        return

    if end_at <= now_utc():
        await interaction.response.send_message(
            "마감일시는 미래로 설정해 주세요.", ephemeral=True
        )
        return

    async with db.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO votes(title, options, creator_id, end_at)
            VALUES($1, $2::jsonb, $3, $4)
            RETURNING id
        """, 제목.strip(), json.dumps(options, ensure_ascii=False),
             str(interaction.user.id), end_at)

    if not PUBLIC_URL:
        await interaction.response.send_message(
            "투표는 생성됐지만 PUBLIC_BASE_URL 환경 변수가 없습니다.",
            ephemeral=True
        )
        return

    link = f"{PUBLIC_URL}/vote/{row['id']}"

    await interaction.response.send_message(
        f"🗳️ **{escape(제목.strip())}**\n"
        f"마감: {kst_time(end_at)} (한국 시간)\n{link}"
    )


# =========================
# FastAPI 웹사이트
# =========================
@asynccontextmanager
async def lifespan(app):
    await init_db()

    task = None
    if TOKEN:
        task = asyncio.create_task(bot.start(TOKEN))
    else:
        print("주의: DISCORD_TOKEN이 설정되지 않았습니다.")

    try:
        yield
    finally:
        if task:
            await bot.close()
            try:
                await task
            except Exception:
                pass

        if db:
            await db.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET or "CHANGE_ME_BEFORE_DEPLOY",
    same_site="lax",
    https_only=PUBLIC_URL.startswith("https://"),
    max_age=604800
)


# =========================
# 웹 디자인
# =========================
CSS = """
*{box-sizing:border-box}
body{
 margin:0;background:#0b0e14;color:#f4f7fb;
 font-family:Arial,'Malgun Gothic',sans-serif;min-height:100vh
}
.wrap{max-width:820px;margin:auto;padding:35px 18px}
.narrow{max-width:650px}
.panel,.poll{
 background:#141a24;border:1px solid #293344;
 border-radius:17px;padding:24px;margin-bottom:14px
}
h1{font-size:32px;margin:10px 0}
h2{font-size:21px;margin:0 0 12px}
.muted{color:#a2adbd;font-size:14px;line-height:1.7}
.eyebrow{color:#4388ff;font-size:11px;font-weight:bold;letter-spacing:3px}
.button{
 display:inline-block;background:#4388ff;color:white;
 border:0;border-radius:10px;padding:13px 18px;
 font-size:15px;font-weight:bold;text-decoration:none;cursor:pointer
}
.button:disabled{opacity:.6}
.secondary{background:#293344}
.account{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
header{display:flex;justify-content:space-between;gap:15px;margin-bottom:25px}
.poll{display:block;text-decoration:none;color:white}
.poll:hover{border-color:#4388ff}
.pill{display:inline-block;background:#173b2b;color:#83e3ad;
 padding:5px 10px;border-radius:99px;font-size:12px}
.closed{background:#34313c;color:#d2c7e9}
.choice{
 display:flex;gap:12px;align-items:center;padding:15px;
 border:1px solid #293344;border-radius:11px;
 margin:10px 0;background:#101620;cursor:pointer
}
.choice:has(input:checked){border-color:#4388ff;background:#15243b}
.choice input{accent-color:#4388ff;width:17px;height:17px}
.result{margin:22px 0}
.resulttop{display:flex;justify-content:space-between;gap:10px;margin-bottom:8px}
.bar{height:9px;background:#293344;border-radius:20px;overflow:hidden}
.fill{height:100%;background:#4388ff}
.notice{padding:13px;background:#1b2637;border-radius:10px;line-height:1.6}
.back{display:inline-block;color:#a2adbd;text-decoration:none;margin-bottom:16px}
@media(max-width:580px){header{display:block}.account{margin-top:16px}.panel{padding:18px}}
"""


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = request.session.get("user")

    account = (
        f'<span class="muted">로그인: {escape(user["username"])}</span>'
        '<a class="button secondary" href="/logout">로그아웃</a>'
        if user else '<a class="button" href="/login">Discord로 로그인</a>'
    )

    return HTMLResponse(f"""
    <!doctype html>
    <html lang="ko"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>투표</title><style>{CSS}</style></head>
    <body><main class="wrap">
    <header><div><div class="eyebrow">DISCORD VOTING</div>
    <h1>🗳️ 투표</h1><p class="muted">마감 전에는 투표 결과가 공개되지 않습니다.</p>
    </div><div class="account">{account}</div></header>
    <section id="list"><p class="muted">불러오는 중...</p></section>
    </main>
    <script>
    async function load(){{
      const box=document.getElementById('list');
      try{{
        const r=await fetch('/api/votes');
        const data=await r.json();box.replaceChildren();
        if(!data.votes.length){{
          box.textContent='아직 투표가 없습니다.';return;
        }}
        data.votes.forEach(v=>{{
          const a=document.createElement('a');a.className='poll';
          a.href='/vote/'+v.id;
          const h=document.createElement('h2');h.textContent=v.title;
          const s=document.createElement('span');
          s.className='pill'+(v.closed?' closed':'');
          s.textContent=v.closed?'투표 종료':'투표 진행 중';
          const p=document.createElement('p');p.className='muted';
          p.textContent='마감: '+v.end_at_kst+' (KST)';
          a.append(h,s,p);box.append(a);
        }});
      }}catch(e){{box.textContent='목록을 불러오지 못했습니다.';}}
    }}
    load();
    </script></body></html>
    """)


@app.get("/login")
async def login(request: Request):
    if not CLIENT_ID or not CLIENT_SECRET or not GUILD_ID:
        raise HTTPException(500, "Discord OAuth 환경 변수를 확인해 주세요.")

    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": f"{get_base(request)}/callback",
        "response_type": "code",
        "scope": "identify guilds.members.read",
        "state": state,
        "prompt": "consent"
    }
    return RedirectResponse(
        "https://discord.com/oauth2/authorize?" + urlencode(params)
    )


@app.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None
):
    if error:
        raise HTTPException(400, "Discord 로그인이 취소되었습니다.")

    expected = request.session.get("oauth_state")
    if not code or not state or not expected:
        raise HTTPException(400, "로그인을 다시 시도해 주세요.")

    if not secrets.compare_digest(state, expected):
        raise HTTPException(400, "로그인 보안 확인에 실패했습니다.")

    redirect_uri = f"{get_base(request)}/callback"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

        if response.status_code != 200:
            raise HTTPException(400, "Discord 토큰 발급에 실패했습니다.")

        token = response.json()["access_token"]

        me_response = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {token}"}
        )
        if me_response.status_code != 200:
            raise HTTPException(400, "Discord 계정 확인에 실패했습니다.")

        me = me_response.json()

    try:
        is_member = await get_member_access_token(token)
    except httpx.HTTPError:
        raise HTTPException(502, "Discord 서버 가입 여부 확인에 실패했습니다.")

    if not is_member:
        raise HTTPException(403, "지정된 Discord 서버의 멤버만 투표할 수 있습니다.")

    request.session.clear()
    request.session["user"] = {
        "id": str(me["id"]),
        "username": me.get("global_name") or me.get("username") or "Discord 사용자"
    }
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/api/votes")
async def list_votes():
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id,title,end_at FROM votes ORDER BY created_at DESC"
        )

    return {"votes": [
        {
            "id": row["id"],
            "title": row["title"],
            "closed": closed(row["end_at"]),
            "end_at_kst": kst_time(row["end_at"])
        }
        for row in rows
    ]}


@app.get("/vote/{vote_id}", response_class=HTMLResponse)
async def vote_page(vote_id: int):
    async with db.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM votes WHERE id=$1)", vote_id
        )
    if not exists:
        raise HTTPException(404, "투표를 찾을 수 없습니다.")

    return HTMLResponse(f"""
    <!doctype html><html lang="ko"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>투표하기</title><style>{CSS}</style></head>
    <body><main class="wrap narrow"><a class="back" href="/">← 투표 목록</a>
    <section id="app" class="panel"><p class="muted">투표를 불러오는 중...</p></section>
    </main>
    <script>
    const id={vote_id};
    async function render(){{
      const root=document.getElementById('app');
      try{{
        const [vr,mr]=await Promise.all([
          fetch('/api/vote/'+id),fetch('/api/me')
        ]);
        const v=await vr.json(), me=await mr.json();
        root.replaceChildren();
        if(!vr.ok){{root.textContent=v.detail||'투표를 찾을 수 없습니다.';return;}}
        const eyebrow=document.createElement('div');eyebrow.className='eyebrow';
        eyebrow.textContent=v.closed?'VOTING CLOSED':'VOTING';
        const h=document.createElement('h1');h.textContent=v.title;
        const deadline=document.createElement('p');deadline.className='muted';
        deadline.textContent='마감: '+v.end_at_kst+' (KST)';
        root.append(eyebrow,h,deadline);

        if(!me.logged_in){{
          const p=document.createElement('p');p.className='notice';
          p.textContent='투표하려면 Discord 계정으로 로그인해 주세요.';
          const a=document.createElement('a');a.className='button';
          a.href='/login';a.textContent='Discord로 로그인';
          root.append(p,a);return;
        }}

        if(v.closed){{
          const pill=document.createElement('span');pill.className='pill closed';
          pill.textContent='투표 종료';root.append(pill);
          const title=document.createElement('h2');title.textContent='최종 투표 결과';
          const total=document.createElement('p');total.className='muted';
          total.textContent='총 '+v.total_votes+'표';root.append(title,total);
          v.results.forEach(item=>{{
            const row=document.createElement('div');row.className='result';
            const top=document.createElement('div');top.className='resulttop';
            const name=document.createElement('span');name.textContent=item.option;
            const count=document.createElement('span');count.className='muted';
            count.textContent=item.count+'표 · '+item.percent+'%';
            top.append(name,count);
            const bar=document.createElement('div');bar.className='bar';
            const fill=document.createElement('div');fill.className='fill';
            fill.style.width=item.percent+'%';bar.append(fill);
            row.append(top,bar);root.append(row);
          }});
          return;
        }}

        const pill=document.createElement('span');pill.className='pill';
        pill.textContent='투표 진행 중';root.append(pill);

        if(v.has_voted){{
          const p=document.createElement('p');p.className='notice';
          p.textContent='이미 투표했습니다. 결과는 마감 후 공개됩니다.';
          root.append(p);return;
        }}

        const choices=document.createElement('div');
        let selected=null;
        v.options.forEach((option,index)=>{{
          const label=document.createElement('label');label.className='choice';
          const input=document.createElement('input');
          input.type='radio';input.name='choice';input.value=index;
          input.addEventListener('change',()=>{{
            selected=index;
            document.querySelectorAll('.choice').forEach(x=>x.classList.remove('selected'));
            label.classList.add('selected');
          }});
          const span=document.createElement('span');span.textContent=option;
          label.append(input,span);choices.append(label);
        }});

        const button=document.createElement('button');
        button.className='button';button.textContent='투표하기';
        button.addEventListener('click',async()=>{{
          if(selected===null){{alert('선택지를 골라 주세요.');return;}}
          button.disabled=true;button.textContent='제출 중...';
          try{{
            const r=await fetch('/api/vote/'+id+'/cast', {{
              method:'POST',
              headers:{{'Content-Type':'application/json'}},
              body:JSON.stringify({{option_index:selected}})
            }});
            const data=await r.json();
            if(!r.ok){{alert(data.detail||'투표에 실패했습니다.');render();return;}}
            render();
          }}catch(e){{alert('서버 연결에 실패했습니다.');button.disabled=false;button.textContent='투표하기';}}
        }});

        const note=document.createElement('p');note.className='muted';
        note.textContent='투표가 끝날 때까지 득표수와 결과는 공개되지 않습니다.';
        root.append(choices,button,note);
      }}catch(e){{root.textContent='불러오지 못했습니다. 새로고침해 주세요.';}}
    }}
    render();
    </script></body></html>
    """)


@app.get("/api/me")
async def api_me(request: Request):
    user = request.session.get("user")
    return {"logged_in": bool(user), "username": user["username"] if user else None}


@app.get("/api/vote/{vote_id}")
async def vote_info(vote_id: int, request: Request):
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id,title,options,end_at FROM votes WHERE id=$1", vote_id
        )
        if not row:
            raise HTTPException(404, "투표를 찾을 수 없습니다.")

        user = request.session.get("user")
        has_voted = False
        if user:
            has_voted = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM ballots WHERE vote_id=$1 AND user_id=$2)",
                vote_id, str(user["id"])
            )

        options = row["options"]
        if isinstance(options, str):
            options = json.loads(options)

        result = {
            "id": row["id"],
            "title": row["title"],
            "options": options,
            "end_at_kst": kst_time(row["end_at"]),
            "closed": closed(row["end_at"]),
            "has_voted": bool(has_voted)
        }

        # 마감 전에는 결과 필드 자체를 응답하지 않음.
        if closed(row["end_at"]):
            rows = await conn.fetch("""
                SELECT option_index, COUNT(*) AS amount
                FROM ballots WHERE vote_id=$1 GROUP BY option_index
            """, vote_id)
            counts = {r["option_index"]: r["amount"] for r in rows}
            total = sum(counts.values())
            result["total_votes"] = total
            result["results"] = [
                {
                    "option": option,
                    "count": counts.get(i, 0),
                    "percent": round(counts.get(i, 0) * 100 / total, 1) if total else 0
                }
                for i, option in enumerate(options)
            ]

        return JSONResponse(result)


@app.post("/api/vote/{vote_id}/cast")
async def cast_vote(vote_id: int, request: Request):
    user = await require_login(request)

    try:
        body = await request.json()
        option_index = int(body["option_index"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, "선택지를 올바르게 선택해 주세요.")

    async with db.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT options,end_at FROM votes WHERE id=$1 FOR UPDATE",
                vote_id
            )
            if not row:
                raise HTTPException(404, "투표를 찾을 수 없습니다.")
            if closed(row["end_at"]):
                raise HTTPException(409, "투표가 종료되었습니다.")

            options = row["options"]
            if isinstance(options, str):
                options = json.loads(options)

            if option_index < 0 or option_index >= len(options):
                raise HTTPException(400, "유효하지 않은 선택지입니다.")

            try:
                await conn.execute("""
                    INSERT INTO ballots(vote_id,user_id,option_index)
                    VALUES($1,$2,$3)
                """, vote_id, str(user["id"]), option_index)
            except asyncpg.UniqueViolationError:
                raise HTTPException(409, "이 Discord 계정은 이미 투표했습니다.")

    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("PORT", "10000")))
