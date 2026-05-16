# Relatório de Implementação: QR Code & Integração Evolution API

Este documento detalha a arquitetura de conexão entre a plataforma **Consigo**, o **Agente WhatsApp** e a **Evolution API**, focando na geração de QR Code, gestão de instâncias e protocolos de recuperação.

## 1. Arquitetura da Integração

A comunicação segue o fluxo:
`Plataforma Consigo (Frontend/Backend)` <-> `Agente WhatsApp (Python/FastAPI)` <-> `Evolution API (Baileys)`

### Componentes Chave:
- **Evolution API:** Gerencia as sessões reais do WhatsApp.
- **Agente WhatsApp:** Atua como um middleware que simplifica a API da Evolution e adiciona lógica de estado (State Machine) e banco de dados local.
- **Consigo Backend:** Gerencia os registros de PDV e solicita ao Agente a criação de conexões.

---

## 2. Endpoints Críticos (Agente WhatsApp)

Para o funcionamento do QR Code, o Agente **deve** expor as seguintes rotas em `app/integration.py`:

| Método | Rota | Descrição |
| :--- | :--- | :--- |
| **POST** | `/v1/integration/instances` | Cria a instância na Evolution e registra no DB local. |
| **GET** | `/v1/integration/instances/{name}/status` | Retorna o estado bruto da conexão (CONNECTED/DISCONNECTED). |
| **GET** | `/v1/integration/instances/{name}/qr` | Retorna o objeto de pareamento (base64) da Evolution. |
| **DELETE** | `/v1/integration/instances/{name}` | Desloga e remove a instância da Evolution. |

---

## 3. Protocolos de Resposta (Payload)

A falha mais comum é o **desencontro de formato**. A Consigo espera que o Agente repasse o objeto **bruto** da Evolution para garantir a renderização do QR Code.

### Exemplo de Resposta do QR Code:
```json
{
  "code": "1@...",
  "base64": "data:image/png;base64,iVBORw0KGgo...",
  "count": 0
}
```
*Nota: Se o Agente encapsular este objeto em outra chave (ex: `{"qr": {...}}`), o frontend da Consigo não conseguirá renderizar a imagem.*

---

## 4. Histórico de Falhas e Soluções (Troubleshooting)

### A. Erro 403 Forbidden
- **Causa:** Token da Evolution (`EVOLUTION_TOKEN`) inválido ou ausente no `.env`.
- **Sintoma:** Log do Agente mostra erro ao tentar `POST /instance/create`.
- **Restauração:** Verificar as variáveis de ambiente e garantir que o header `apikey` está sendo enviado corretamente no `evolution.py`.

### B. Erro 404 Not Found
- **Causa:** Rotas não registradas no `main.py` ou `integration.py` após um revert de código.
- **Sintoma:** Backend da Consigo reporta "Agent API Error: Not Found".
- **Restauração:** Garantir que `app.include_router(integration_router)` está presente no `main.py`.

### C. QR Code não renderiza (mesmo com 200 OK)
- **Causa:** Mudança no nome do método ou formato do JSON.
- **Sintoma:** O log mostra que a imagem foi enviada, mas a tela da Consigo fica em branco.
- **Restauração:** Validar se o Agente está retornando o JSON direto da Evolution. Verificar se não houve confusão entre `get_connection_state` (nome interno) e `get_connection_status` (nome comum).

---

## 5. Procedimento de Restauração em Caso de Crash

Caso o servidor pare de responder ou as rotas sumam:

1. **Check de Sanidade do `main.py`:**
   - Verifique se os roteadores `admin_router` e `integration_router` estão incluídos.
   - Verifique se o `extract_payload` está tratando listas `[]` e objetos `{}`.

2. **Check de `integration.py`:**
   - Confirme se as 4 rotas de instância (POST, GET status, GET qr, DELETE) existem.

3. **Check de `evolution.py`:**
   - Garanta que a URL base não termina com `/` duplicada e que o `apikey` está no `__init__`.

---
*Relatório gerado em 15/05/2026 para fins de auditoria e manutenção técnica.*
