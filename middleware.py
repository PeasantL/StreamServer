# middleware.py
from ipaddress import ip_address
from fastapi import HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from config import config

def is_ip_allowed(remote_addr: str, allowed_ips: list) -> bool:
    try:
        client_ip = ip_address(remote_addr)
        for allowed_ip in allowed_ips:
            try:
                if client_ip == ip_address(allowed_ip):
                    return True
            except ValueError:
                continue
    except ValueError:
        pass
    return False


async def whitelist_middleware(request: Request, call_next):
    client_ip = request.headers.get("x-forwarded-for", request.client.host)
    
    if client_ip:
        # Handle multiple IPs in the x-forwarded-for header
        client_ip = client_ip.split(",")[0].strip()
    
    if not is_ip_allowed(client_ip, config.allowed_ips):
        raise HTTPException(
            status_code=403,
            detail=f"Access denied for IP {client_ip}"
        )
    
    return await call_next(request)


def add_cors_middleware(app):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
