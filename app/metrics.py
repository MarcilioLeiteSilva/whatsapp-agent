from prometheus_client import Counter, Histogram

WEBHOOK_RECEIVED = Counter("wa_webhook_received_total", "Webhooks recebidos")
WEBHOOK_IGNORED = Counter("wa_webhook_ignored_total", "Webhooks ignorados", ["reason"])
MSG_PROCESSED = Counter("wa_messages_processed_total", "Mensagens processadas")
MSG_SENT_OK = Counter("wa_messages_sent_ok_total", "Mensagens enviadas com sucesso")
MSG_SENT_ERR = Counter("wa_messages_sent_err_total", "Erros ao enviar mensagens")
LEAD_FIRST_CONTACT = Counter("wa_lead_first_contact_total", "Primeiros contatos registrados")
LEAD_INTENT_MARKED = Counter("wa_lead_intent_marked_total", "Intenções marcadas")
LEAD_SAVED = Counter("wa_lead_saved_total", "Leads salvos (handoff)")

WEBHOOK_LATENCY = Histogram("wa_webhook_latency_seconds", "Latência do webhook")
