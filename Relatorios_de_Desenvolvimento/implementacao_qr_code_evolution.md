# Relatório de Implementação: QR Code & Integração Evolution API

Este documento detalha a infraestrutura de conexão e serve como guia de recuperação em caso de falhas no Agente WhatsApp.

## 1. Arquitetura da Integração
`Plataforma Consigo` <-> `Agente WhatsApp (Python)` <-> `Evolution API`

---

## 2. Parâmetros Necessários no Banco de Dados

O Agente utiliza o PostgreSQL (via SQLAlchemy) para persistir as instâncias. Se estas tabelas não estiverem corretas, o comando de conexão (QR Code) falhará com erro 500.

### Tabela `agents` (Modelo `Agent`)
É onde as instâncias são registradas para que o robô saiba a quem responder.
- **id (String/PK):** Nome da instância (ex: `c_80463ba2`).
- **instance (String):** **CRÍTICO!** Deve se chamar exatamente `instance`. Armazena o ID da instância na Evolution.
- **client_id (String):** ID do Tenant na Consigo.
- **name (String):** Nome amigável do agente.

### Tabela `leads` (Modelo `Lead`)
Registra as conversas e o estado de cada lojista.
- **from_number:** Número do WhatsApp do lojista.
- **status:** Estado atual da conversa.
- **instance:** Vincula a conversa à instância correta.

---

## 3. Variáveis de Ambiente Obrigatórias (.env)
Se estas chaves estiverem erradas, o QR Code não gera (403 Forbidden).
- `EVOLUTION_TOKEN`: Chave da API Evolution.
- `INTEGRATION_KEY`: Chave de segurança entre Consigo e Agente.
- `DATABASE_URL`: Conexão com o PostgreSQL.

---

## 4. Alterações Críticas que Quebram o Código (NUNCA ALTERAR)

### 🔴 Estrutura do JSON de Resposta (QR e Status)
A Consigo espera o objeto **bruto** da Evolution.
- **Errado:** `return {"ok": True, "qr": res}` (Quebra a renderização).
- **Correto:** `return res` (Onde `res` já contém `base64` e `code`).

### 🔴 Nome de Atributos do Modelo Agent
- **Erro comum:** Tentar usar `Agent.instance_name`.
- **Fato:** O banco de dados está mapeado como `Agent.instance`. Mudar isso causa erro 500 no `POST /instances`.

### 🔴 Indentação em Handlers de IA
O arquivo `rules.py` utiliza blocos `try/except` para extração de JSON via IA. Se a indentação for alterada, o robô para de processar as quantidades informadas pelos lojistas.

---

## 5. Protocolo de Recuperação (Checklist)

1. **Erro 500 ao conectar?** Verifique se o código está usando `Agent.instance` (e não `instance_name`).
2. **QR Code não aparece (mas dá 200 OK)?** Verifique se a rota `/qr` no `integration.py` está retornando o objeto bruto (`return res`).
3. **Robô não responde?** Verifique se o `main.py` não está bloqueando o número por estar no modo de "Pausa" (Silêncio pós-acerto).

---
*Atualizado em 15/05/2026 - Versão de Estabilidade Garantida.*
