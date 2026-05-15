@router.post("/instances", response_model=InstanceResponse)
async def create_instance(data: InstanceCreate, _ = Depends(verify_key)):
    """Cria uma nova instância na Evolution e registra no banco local."""
    
    with SessionLocal() as db:
        # 1. Garantir que o cliente existe
        client = db.execute(select(Client).where(Client.id == data.client_id)).scalar_one_or_none()
        if not client:
            client = Client(id=data.client_id, name=data.client_name)
            db.add(client)
            db.commit()

    try:
        # 1. Garantir que a instância existe na Evolution
        try:
            status = await evo.get_instance_status(data.instance_name)
            logger.info(f"Instance {data.instance_name} already exists. Status: {status.get('instance', {}).get('state')}")
        except Exception:
            # Se não existir, cria
            await evo.create_instance(data.instance_name)
            logger.info(f"Instance {data.instance_name} created successfully.")
        
        # 2. Configurar Webhook
        await evo.set_webhook(data.instance_name, f"{AGENT_BASE_URL}/webhook")
        
        # 3. Obter QR Code (pode falhar se já estiver conectado)
        qr_data = None
        try:
            qr_data = await evo.get_qrcode(data.instance_name)
        except Exception as e:
            logger.warning(f"Could not get QR Code (maybe already connected): {e}")

        # 4. Registrar no Banco Local
        with SessionLocal() as db:
            agent = db.execute(select(Agent).where(Agent.name == data.instance_name)).scalar_one_or_none()
            if not agent:
                agent = Agent(
                    name=data.instance_name,
                    client_id=data.client_id,
                    evolution_base_url=evo.base_url,
                    status="active"
                )
                db.add(agent)
                db.commit()
            
            return {
                "instance": data.instance_name,
                "status": "active",
                "qrcode": qr_data.get("base64") if qr_data else None
            }

    except Exception as e:
        logger.error(f"Error creating instance: {e}")
        raise HTTPException(status_code=500, detail=str(e))
