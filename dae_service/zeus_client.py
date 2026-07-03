# -*- coding: utf-8 -*-
"""
Client per l'API Zeus di SolaX Cloud (euapi.solaxcloud.com).

L'API pubblica SolaX v2 (api.solaxcloud.com/getRealtimeInfo) restituisce
solo powerdc1..4 per gli inverter Hybrid. I dati per-MPPT completi
(tensione, corrente, potenza per ogni MPPT/PV) sono disponibili solo
tramite l'API interna Zeus usata dal portale web SolaX Cloud.

Questo client implementa:
- Autenticazione con password MD5 e JWT token
- Crittografia AES-CBC per request/response (crytoVer=1)
- Endpoint devInverter/recentData per dati real-time per-MPPT
"""

import base64
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# AES key/IV usate dall'app web SolaX Cloud (crytoVer=1)
_AES_KEY = b"hj7x22H$yuBI0456"
_AES_IV = b"NIfb&74GUY86Gfgh"

# Login endpoint
_LOGIN_URL = "https://euapi.solaxcloud.com/unionUser/web/v2/public/login"
_ZEUS_BASE = "https://euapi.solaxcloud.com/zeus/v1"


def _aes_encrypt(plaintext: str) -> str:
    """Cifra con AES-CBC PKCS7."""
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
    except ImportError:
        from Cryptodome.Cipher import AES
        from Cryptodome.Util.Padding import pad

    cipher = AES.new(_AES_KEY, AES.MODE_CBC, _AES_IV)
    ct = cipher.encrypt(pad(plaintext.encode("utf-8"), AES.block_size))
    return base64.b64encode(ct).decode("utf-8")


def _aes_decrypt(ciphertext_b64: str) -> str:
    """Decifra da AES-CBC PKCS7."""
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError:
        from Cryptodome.Cipher import AES
        from Cryptodome.Util.Padding import unpad

    ct = base64.b64decode(ciphertext_b64)
    cipher = AES.new(_AES_KEY, AES.MODE_CBC, _AES_IV)
    return unpad(cipher.decrypt(ct), AES.block_size).decode("utf-8")


class ZeusClient:
    """Client per l'API Zeus interna di SolaX Cloud."""

    def __init__(
        self,
        email: Optional[str] = None,
        password: Optional[str] = None,
        site_id: Optional[str] = None,
    ):
        self.email = email or os.environ.get("SOLAX_CLOUD_EMAIL", "")
        self.password = password or os.environ.get("SOLAX_CLOUD_PASSWORD", "")
        self.site_id = site_id or os.environ.get("SOLAX_SITE_ID", "1905618003408482305")
        self._token: Optional[str] = None
        self._token_expires: float = 0
        self._session = requests.Session()

    @property
    def is_available(self) -> bool:
        """True se le credenziali Zeus sono configurate."""
        return bool(self.email and self.password)

    def _ensure_token(self) -> bool:
        """Ottiene o rinnova il JWT token. Ritorna True se il token e' valido."""
        if self._token and time.time() < self._token_expires:
            return True
        return self._login()

    def _login(self) -> bool:
        """Autentica e ottiene un nuovo JWT token."""
        if not self.is_available:
            logger.warning("Zeus: credenziali non configurate")
            return False

        try:
            password_md5 = hashlib.md5(self.password.encode()).hexdigest()
            # The account list shows loginName may differ from email
            # Try the username part first (type=4 installer), then full email
            login_names = []
            if "@" in self.email:
                login_names.append(self.email.split("@")[0])
                login_names.append(self.email)
            else:
                login_names.append(self.email)

            for login_name in login_names:
                resp = self._session.post(
                    _LOGIN_URL,
                    json={
                        "loginName": login_name,
                        "password": password_md5,
                        "route": 1,
                    },
                    headers={"Content-Type": "application/json", "Lang": "en_US"},
                    timeout=15,
                )
                data = resp.json()
                if data.get("code") == 0 and data.get("result", {}).get("token"):
                    self._token = data["result"]["token"]
                    # Token JWT valido ~12h, rinnova dopo 10h
                    self._token_expires = time.time() + 10 * 3600
                    logger.info(f"Zeus: login riuscito ({login_name})")
                    return True

                msg = data.get("message", "")
                logger.debug(f"Zeus: login fallito per '{login_name}': {msg}")
                if "locked" in msg.lower():
                    logger.warning(f"Zeus: account bloccato — {msg}")
                    return False

            logger.error("Zeus: login fallito con tutte le credenziali")
            return False

        except Exception as e:
            logger.error(f"Zeus: errore login — {e}")
            return False

    def _make_headers(self) -> Dict[str, str]:
        """Costruisce gli header per le request Zeus."""
        ts = int(time.time() * 1000)
        return {
            "Accept": "application/json",
            "token": self._token or "",
            "Lang": "en_US",
            "deviceType": "3",
            "deviceId": f"{uuid.uuid4().hex[:8]}-{ts}",
            "version": "green",
            "websiteType": "0",
            "source": "0",
            "x-transaction-id": f"{uuid.uuid4().hex[:8]}-{ts}",
            "queryTime": time.strftime("%Y-%m-%d %H:%M:%S"),
            "Permission-Version": "v7.2.0",
            "platform": "4",
            "crytoVer": "1",
        }

    def _zeus_get(self, endpoint: str, params: dict) -> Optional[dict]:
        """GET request cifrata all'API Zeus."""
        if not self._ensure_token():
            return None
        try:
            ts = int(time.time() * 1000)
            req_id = uuid.uuid4().hex[:8]
            params.update({"timeStamp": ts, "requestId": req_id})
            encrypted = _aes_encrypt(json.dumps(params))

            import urllib.parse
            url = f"{_ZEUS_BASE}/{endpoint}?data={urllib.parse.quote(encrypted)}"
            resp = self._session.get(url, headers=self._make_headers(), timeout=15)
            resp_json = resp.json()

            if "data" not in resp_json:
                code = resp_json.get("code", "?")
                if code == 10020002:
                    logger.info("Zeus: token scaduto, ri-autenticazione...")
                    self._token = None
                    self._token_expires = 0
                    if self._ensure_token():
                        return self._zeus_get(endpoint, params)
                logger.error(f"Zeus: errore API {endpoint}: {resp_json}")
                return None

            decrypted = _aes_decrypt(resp_json["data"])
            return json.loads(decrypted)

        except Exception as e:
            logger.error(f"Zeus: errore {endpoint}: {e}")
            return None

    def get_inverter_mppt_data(self, sn: str) -> Optional[Dict]:
        """
        Ottiene dati real-time per-MPPT di un inverter.

        Returns:
            Dict con:
            - brief: {acPower, pvPower, ratedPower, ...}
            - pv.MPPT: [{name, voltage, current, power}, ...]
            - pv.PV: [{name, voltage, current, power}, ...]  (stringhe individuali)
        """
        data = self._zeus_get(
            "devInverter/recentData",
            {"sn": sn, "siteId": self.site_id},
        )
        if not data:
            return None
        return data.get("result")

    def get_all_inverters_mppt(
        self, inverter_sns: List[str]
    ) -> Dict[str, Dict]:
        """
        Ottiene dati per-MPPT per tutti gli inverter dell'impianto.

        Returns:
            Dict {sn: {brief, pv: {MPPT: [...], PV: [...]}}}
        """
        results = {}
        for sn in inverter_sns:
            data = self.get_inverter_mppt_data(sn)
            if data:
                results[sn] = data
            time.sleep(0.2)  # Rate limiting
        return results

    def extract_mppt_flat(
        self, inverter_sns: List[str]
    ) -> Tuple[List[Dict], Dict[str, Dict]]:
        """
        Estrae dati per-MPPT in formato piatto compatibile con StringAnalyzer.

        Returns:
            (inverter_list, zeus_mppt_data)
            - inverter_list: lista di dict [{inverterSN, acpower, powerdc1..N, vdc1..N}]
              per backward compatibility con l'API pubblica
            - zeus_mppt_data: dict {sn: {mppt: [...], pv: [...]}} con dati completi
        """
        all_data = self.get_all_inverters_mppt(inverter_sns)
        if not all_data:
            return [], {}

        inverter_list = []
        zeus_mppt_data = {}

        for sn, data in all_data.items():
            brief = data.get("brief", {})
            pv_section = data.get("pv", {})
            mppt_list = pv_section.get("MPPT", [])
            pv_list = pv_section.get("PV", [])

            inv_dict = {
                "inverterSN": sn,
                "acpower": brief.get("acPower", 0) or 0,
                "yieldtoday": brief.get("acYieldToday", 0) or 0,
                "feedinpower": 0,
                "inverterType": brief.get("inverterType", 100),
            }

            # Map MPPT data to powerdc/vdc fields
            for idx, mppt in enumerate(mppt_list):
                i = idx + 1
                inv_dict[f"powerdc{i}"] = mppt.get("power", 0) or 0
                inv_dict[f"vdc{i}"] = mppt.get("voltage", 0) or 0
                inv_dict[f"idc{i}"] = mppt.get("current", 0) or 0

            # Battery data for Hybrid
            batteries = data.get("batteries", [])
            if batteries and isinstance(batteries[0], dict):
                inv_dict["soc"] = batteries[0].get("soc", 0) or 0
                inv_dict["batPower"] = batteries[0].get("power", 0) or 0

            inverter_list.append(inv_dict)
            zeus_mppt_data[sn] = {
                "mppt": mppt_list,
                "pv": pv_list,
                "n_mppt": len(mppt_list),
            }

        return inverter_list, zeus_mppt_data
