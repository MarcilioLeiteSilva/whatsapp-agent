import os
from datetime import datetime

import pytz
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .lead_logger import get_last_leads

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

router = APIRouter()

BR_TZ = pytz.timezone("America/Sao_Paulo")


def _auth_ok(request: Request) -> bool:
    if not ADMIN_TOKEN:
        return True  # dev: sem token, não bloqueia
    token = request.headers.get("x-admin-token") or request.query_params.get("token")
    return token == ADMIN_TOKEN


def format_dt(value) -> str:
    """
    Formata datetime/ISO string para America/Sao_Paulo.
    Exibe dd/mm/YYYY HH:MM:SS.
    """
    if not value:
        return ""
    try:
        if isinstance(value, str):
            # aceita "2026-02-20 19:22:38.809885+00:00" e ISO padrão
            v = value.strip().replace("Z", "+00:00")
            # se vier com espaço em vez de T, fromisoformat aceita
            dt = datetime.fromisoformat(v)
        else:
            dt = value

        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)

        dt_br = dt.astimezone(BR_TZ)
        return dt_br.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return str(value)


@router.get("/admin/leads", response_class=HTMLResponse)
async def admin_leads(request: Request, q: str = "", limit: int = 50):
    if not _auth_ok(request):
        return HTMLResponse("<h3>Unauthorized</h3>", status_code=401)

    leads = get_last_leads(limit=min(limit, 200))

    if q:
        ql = q.lower()
        leads = [
            l for l in leads
            if ql in (l.get("nome") or "").lower()
            or ql in (l.get("telefone") or "").lower()
            or ql in (l.get("assunto") or "").lower()
            or ql in (l.get("from_number") or "").lower()
            or ql in (l.get("intent_detected") or "").lower()
            or ql in (l.get("status") or "").lower()
            or ql in (l.get("origem") or "").lower()
        ]

    rows = []
    for l in leads:
        rows.append(f"""
        <tr class="border-t">
          <td class="p-3">{l.get('id','')}</td>
          <td class="p-3">{l.get('from_number','')}</td>
          <td class="p-3">{l.get('nome','')}</td>
          <td class="p-3">{l.get('telefone','')}</td>
          <td class="p-3">{l.get('assunto','')}</td>
          <td class="p-3">{l.get('status','')}</td>
          <td class="p-3">{l.get('origem','')}</td>
          <td class="p-3">{format_dt(l.get('created_at'))}</td>
          <td class="p-3">{l.get('intent_detected','')}</td>
        </tr>
        """)

    html = f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <meta http-equiv="refresh" content="10">
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Leads</title>
        <script src="https://cdn.tailwindcss.com"></script>
      </head>
      <body class="bg-slate-50">
        <div class="max-w-6xl mx-auto p-6">
          <div class="flex items-center justify-between mb-4">
            <h1 class="text-2xl font-semibold">Leads</h1>
            <form class="flex gap-2" method="get">
              <input name="q" value="{q}" placeholder="Buscar (nome/telefone/assunto)" class="w-96 px-3 py-2 border rounded-lg"/>
              <input name="limit" value="{limit}" class="w-24 px-3 py-2 border rounded-lg"/>
              <button class="px-4 py-2 rounded-lg bg-black text-white">Filtrar</button>
            </form>
          </div>

          <div class="bg-white rounded-xl shadow-sm border overflow-x-auto">
            <table class="min-w-full text-sm">
              <thead class="bg-slate-100">
                <tr>
                  <th class="text-left p-3">ID</th>
                  <th class="text-left p-3">From</th>
                  <th class="text-left p-3">Nome</th>
                  <th class="text-left p-3">Telefone</th>
                  <th class="text-left p-3">Assunto</th>
                  <th class="text-left p-3">Status</th>
                  <th class="text-left p-3">Origem</th>
                  <th class="text-left p-3">Criado (BR)</th>
                  <th class="text-left p-3">Intent</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows) if rows else '<tr><td class="p-3" colspan="9">Nenhum lead encontrado.</td></tr>'}
              </tbody>
            </table>
          </div>

          <p class="text-xs text-slate-500 mt-3">
            Atualiza automaticamente a cada 10s. Protegido por ADMIN_TOKEN (query ?token=... ou header x-admin-token).
          </p>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)
