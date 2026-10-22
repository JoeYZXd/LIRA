"""Cancellable LAN-only television display transport; no power operations."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx


class TvDisplaySender:
    def __init__(self, config_path):
        config=json.loads(Path(config_path).read_text(encoding='utf-8'))
        from ipaddress import ip_address
        host=str(ip_address(config['host']))
        if ':' in host:host='['+host+']'
        self.base=f'http://{host}:40100'
        self.launch=f'http://{host}:6095/controller'
        self.token=config['token']
        if not isinstance(self.token,str) or len(self.token)<32:raise ValueError('Invalid TV pairing')

    async def show(self,payload):
        deadline=time.monotonic()+30
        last_launch=-float('inf')
        # Separate requests: never forward the receiver credential to the vendor endpoint.
        async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
            async def request(method,path,body=None):
                response=await client.request(method,self.base+path,json=body,
                    headers={'Authorization':'Bearer '+self.token})
                response.raise_for_status()
                return response.json()
            while time.monotonic()<deadline:
                try:status=await request('GET','/status')
                except httpx.TransportError:
                    if time.monotonic()-last_launch>=10:
                        last_launch=time.monotonic()
                        try:
                            await client.get(self.launch,params={'action':'startapp','type':'packagename','packagename':'org.lira.receiver'})
                        except httpx.TransportError:pass
                    await asyncio.sleep(1)
                    continue
                if not status.get('interactive'):
                    raise RuntimeError('TV screen is off; no wake attempted')
                await request('POST','/show',payload)
                # After submission do not relaunch: a person may have pressed Back/Home.
                for _ in range(15):
                    status=await request('GET','/status')
                    if status.get('rendered_id')==payload['id'] and status.get('foreground'):
                        return status
                    await asyncio.sleep(.2)
                raise RuntimeError('Display not confirmed')
        raise TimeoutError('TV unavailable')


    async def probe(self):
        async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
            response=await client.get(self.base+'/status',headers={'Authorization':'Bearer '+self.token})
            response.raise_for_status()
            status=response.json()
            return {key:status.get(key) for key in ('version','capabilities','interactive','foreground','rendered_id','instance','audio')}

    async def ensure_ready(self):
        """Explicit task preparation, never called by ordinary status queries.

        Launch at most once, then trust only the authenticated receiver status.
        No ADB, IR/power command, or receiver credential at the vendor endpoint.
        """
        return await asyncio.wait_for(self._ensure_ready(), timeout=15)

    async def _ensure_ready(self):
        try:
            status=await self.probe()
        except httpx.TransportError:
            status=None
        if status is not None:
            if status.get('interactive') is not True or status.get('foreground') is True:
                return status
        async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
            try:
                response=await client.get(self.launch,params={
                    'action':'startapp','type':'packagename','packagename':'org.lira.receiver'})
                response.raise_for_status()
            except httpx.TransportError:
                # Some firmware launches successfully but never acknowledges.
                pass
        for _ in range(30):
            try:
                status=await self.probe()
                if status.get('interactive') is False or status.get('foreground') is True:
                    return status
            except httpx.TransportError:
                pass
            await asyncio.sleep(.25)
        raise TimeoutError('TV receiver readiness not confirmed after launch')

    async def clear(self,expected_content_id):
        # This endpoint never launches, wakes, or changes foreground ownership.
        import re
        if not isinstance(expected_content_id,str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}',expected_content_id):
            raise ValueError('Invalid content ID')
        status=await self.probe()
        if 'clear_content_id' not in (status.get('capabilities') or []):raise RuntimeError('Receiver upgrade required')
        if not status.get('interactive') or not status.get('foreground'):raise RuntimeError('Display unavailable or dismissed')
        async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
            response=await client.post(self.base+'/clear',json={'expected_content_id':expected_content_id},
                headers={'Authorization':'Bearer '+self.token})
            response.raise_for_status()
            result=response.json()
            if result.get('cleared_id')!=expected_content_id:raise RuntimeError('Clear not confirmed')
            return result
