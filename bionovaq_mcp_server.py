#!/usr/bin/env python3
"""BionovaQ MCP Server v2 - Session-based multi-user architecture

CRITICAL CHANGES FROM v1:
- NO global variables (AUTH_TOKEN, APP_USER_ID, COMPANY_ID removed)
- SessionManager integration for multi-client support
- All tools return raw JSON dicts (not formatted strings with emoji)
- session_uuid required for authenticated tools
- Each user maintains independent session state

ARCHITECTURE:
- Sessions stored in ./sessions/{uuid}.json
- Each session contains: token, base_url, user_id, company_id, exp
- Tools that need auth: require session_uuid parameter
- Tools without auth: login, logout, list_sessions, get_distributor_by_domain, get_countries, get_languages, check_connection
"""

import os
import sys
import logging
import json
import time
import base64
import httpx
from typing import Dict, Any
from session_manager import SessionManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("bionovaq-mcp-v2")

# Initialize SessionManager
SESSIONS_DIR = os.environ.get("BIONOVA_SESSIONS_DIR", "./sessions")
session_manager = SessionManager(SESSIONS_DIR)

# Initialize FastMCP
from fastmcp import FastMCP
mcp = FastMCP("bionovaq")

# === UTILITY FUNCTIONS ===

def portal_domain_to_api_url(domain: str) -> str:
    """Convert portal domain to API URL

    Examples:
        https://dev.bionovaq.com -> https://api-dev.bionovaq.com
        https://bionovaq.com -> https://api.bionovaq.com
        https://staging.bionovaq.com -> https://api-staging.bionovaq.com
    """
    # Remove protocol and trailing slash
    domain = domain.replace("https://", "").replace("http://", "").rstrip("/")

    # Extract subdomain
    parts = domain.split(".")
    if len(parts) >= 3:  # Has subdomain (e.g., dev.bionovaq.com)
        subdomain = parts[0]
        base_domain = ".".join(parts[1:])
        api_url = f"https://api-{subdomain}.{base_domain}"
    else:  # No subdomain (e.g., bionovaq.com)
        api_url = f"https://api-prod.{domain}"

    return api_url


def parse_jwt_token(token: str) -> Dict[str, Any]:
    """Parse JWT and extract user_id, company_id, profile_id, exp

    Returns dict with: app_user_id, company_id, profile_id, exp, user_data
    """
    if not token:
        return {}

    try:
        parts = token.split(".")
        if len(parts) != 3:
            logger.warning("Malformed JWT token")
            return {}

        payload_b64 = parts[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)

        decoded_payload = base64.urlsafe_b64decode(payload_b64.encode('utf-8'))
        data = json.loads(decoded_payload.decode('utf-8'))

        exp = data.get("exp")
        unique_name_str = data.get("unique_name")
        nameid = data.get("nameid")

        extracted_data = {}
        if exp:
            extracted_data["exp"] = int(exp)

        # Get app_user_id from nameid claim (most reliable)
        if nameid:
            extracted_data["app_user_id"] = str(nameid)

        # Parse unique_name JSON for additional data
        if unique_name_str:
            try:
                user_data = json.loads(unique_name_str)

                # Get company_id from Company.Id in unique_name
                company = user_data.get("Company", {})
                if isinstance(company, dict):
                    company_id = company.get("Id")
                    if company_id:
                        extracted_data["company_id"] = str(company_id)

                # Extract ProfileId from unique_name
                profile_id = user_data.get("ProfileId")
                if profile_id:
                    extracted_data["profile_id"] = str(profile_id)

                # Fallback: use Id from root level if app_user_id not set
                if "app_user_id" not in extracted_data:
                    user_id = user_data.get("Id")
                    if user_id:
                        extracted_data["app_user_id"] = str(user_id)

                extracted_data["user_data"] = user_data
            except json.JSONDecodeError:
                logger.warning("Could not parse unique_name as JSON")

        return extracted_data

    except Exception as e:
        logger.error(f"Error parsing JWT token: {e}", exc_info=True)
        return {}


async def make_api_call(token: str, api_url: str, method: str, endpoint: str,
                       body: Dict[str, Any] = None, query_params: str = "") -> Dict[str, Any]:
    """Make API request and return raw JSON dict

    Returns:
        {"success": True, "data": ..., "status_code": 200} on success
        {"success": False, "error": "...", "status_code": 4xx} on error
    """
    url = f"{api_url}{endpoint}"
    if query_params:
        url = f"{url}?{query_params}"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    logger.info(f"API call: {method} {url}")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "GET":
                response = await client.get(url, headers=headers)
            elif method == "POST":
                response = await client.post(url, headers=headers, json=body or {})
            elif method == "PUT":
                response = await client.put(url, headers=headers, json=body or {})
            elif method == "DELETE":
                # httpx's delete() doesn't support body, use request() instead
                if body:
                    response = await client.request("DELETE", url, headers=headers, json=body)
                else:
                    response = await client.delete(url, headers=headers)
            else:
                return {
                    "success": False,
                    "error": f"Unsupported HTTP method: {method}"
                }

            response.raise_for_status()

            if response.content:
                try:
                    data = response.json()
                    return {
                        "success": True,
                        "data": data,
                        "status_code": response.status_code
                    }
                except json.JSONDecodeError:
                    return {
                        "success": True,
                        "data": response.text,
                        "status_code": response.status_code
                    }
            else:
                return {
                    "success": True,
                    "data": None,
                    "status_code": response.status_code,
                    "message": "No content"
                }

    except httpx.HTTPStatusError as e:
        error_detail = ""
        try:
            error_detail = e.response.text
        except:
            pass
        return {
            "success": False,
            "error": error_detail or str(e),
            "status_code": e.response.status_code
        }
    except httpx.TimeoutException:
        return {
            "success": False,
            "error": "Request timed out after 30 seconds",
            "status_code": 408
        }
    except Exception as e:
        logger.error(f"Request error: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "status_code": 500
        }


# === AUTHENTICATION ENDPOINTS ===

@mcp.tool()
async def login(email: str = "", password: str = "", dist_id: str = "",
               domain: str = "https://dev.bionovaq.com") -> Dict[str, Any]:
    """Authenticate user with BionovaQ and create session

    WORKFLOW: Call this first to obtain session_uuid

    Args:
        email: User email
        password: User password
        dist_id: Distributor ID (optional, can be obtained via get_distributor_by_domain)
        domain: Portal domain (e.g., https://dev.bionovaq.com)
                The API URL is derived automatically from this domain

    Returns:
        {
            "success": True,
            "session_uuid": "...",
            "user_email": "...",
            "app_user_id": "...",
            "company_id": "...",
            "expires_at": "2025-10-28 12:00:00",
            "portal_domain": "https://dev.bionovaq.com"
        }
    """
    # --- inicio función interna ---
    async def get_distributor_id(domain: str) -> str | None:
        api_url = portal_domain_to_api_url(domain)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        body = {"Domain": domain}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{api_url}/api/Distributor/PostByDomain",
                    headers=headers,
                    json=body
                )
                response.raise_for_status()  # lanza error si no es 2xx
                data = response.json()
                return data.get("id")  # devolvemos solo el ID
        except httpx.HTTPError as e:
            logger.error(f"Error al obtener dist_id: {e}")
            return None
    # --- fin función interna ---
    if not email or not password:
        return {
            "success": False,
            "error": "email and password are required"
        }

    # Derive API URL from portal domain (internal, not exposed to user)
    api_url = portal_domain_to_api_url(domain)
    url = f"{api_url}/api/login/login"
    logger.info(f"Login attempt: {email} -> {url}")
    if not dist_id:
        dist_id = await get_distributor_id(domain)
        if not dist_id:
            return {
                "success": False,
                "error": "No se pudo obtener el ID del distribuidor"
            }        
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {
                "Email": email,
                "Password": password,
                "distId": dist_id
            }

            response = await client.post(
                url,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json=payload
            )
            response.raise_for_status()

            data = response.json() if response.content else {}

            token = None
            if isinstance(data, dict):
                token = data.get("token") or data.get("accessToken") or data.get("access_token")
            elif isinstance(data, str):
                token = data

            if not token:
                return {
                    "success": False,
                    "error": "No token in response",
                    "response": data
                }

            # Parse token
            raw_token = token.replace("Bearer ", "").strip()
            parsed_data = parse_jwt_token(raw_token)

            if not parsed_data:
                return {
                    "success": False,
                    "error": "Failed to parse token"
                }

            app_user_id = parsed_data.get("app_user_id", "")
            company_id = parsed_data.get("company_id", "")
            profile_id = parsed_data.get("profile_id", "")
            exp = parsed_data.get("exp")
            user_data = parsed_data.get("user_data", {})

            # Create session
            session_uuid = session_manager.create_session(
                token=raw_token,
                api_url=api_url,
                portal_domain=domain,
                user_email=email,
                app_user_id=app_user_id,
                company_id=company_id,
                profile_id=profile_id,
                exp=exp,
                user_data=user_data
            )

            return {
                "success": True,
                "session_uuid": session_uuid,
                "user_email": email,
                "app_user_id": app_user_id,
                "company_id": company_id,
                "portal_domain": domain,
                "expires_at": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp)) if exp else None,
                "message": "Login successful. Use this session_uuid for subsequent calls."
            }

    except httpx.HTTPStatusError as e:
        error_detail = ""
        try:
            error_detail = e.response.text
        except:
            pass
        return {
            "success": False,
            "error": error_detail or str(e),
            "status_code": e.response.status_code
        }
    except Exception as e:
        logger.error(f"Login error: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }


@mcp.tool()
async def logout(session_uuid: str = "") -> Dict[str, Any]:
    """End session and delete session file

    Args:
        session_uuid: Session UUID from login()

    Returns:
        {"success": True, "message": "Session deleted"}
    """
    if not session_uuid:
        return {
            "success": False,
            "error": "session_uuid is required"
        }

    success = session_manager.delete_session(session_uuid)

    if success:
        return {
            "success": True,
            "message": "Session deleted"
        }
    else:
        return {
            "success": False,
            "error": "Session not found or already deleted"
        }


@mcp.tool()
async def list_sessions() -> Dict[str, Any]:
    """List all active sessions (admin/debug tool)

    Returns:
        {
            "success": True,
            "sessions": [
                {
                    "session_uuid": "...",
                    "user_email": "...",
                    "app_user_id": "...",
                    "company_id": "...",
                    "created_at": "2025-10-28 10:00:00",
                    "expires_at": "2025-10-28 12:00:00"
                }
            ]
        }
    """
    sessions = session_manager.list_sessions()
    return {
        "success": True,
        "sessions": sessions,
        "count": len(sessions)
    }


@mcp.tool()
async def check_connection(domain: str = "https://dev.bionovaq.com") -> Dict[str, Any]:
    """Test API connection without authentication

    Args:
        domain: Portal domain (default: https://dev.bionovaq.com)

    Returns:
        {"success": True, "message": "API is reachable"}
    """
    try:
        api_url = portal_domain_to_api_url(domain)
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(api_url)
            return {
                "success": True,
                "message": "API is reachable",
                "api_url": api_url,
                "status_code": response.status_code
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# === INITIALIZATION ENDPOINTS (7) ===

@mcp.tool()
async def get_distributor_by_domain(domain: str = "") -> Dict[str, Any]:
    """Get distributor information and dist_id by portal domain

    Args:
        domain: Web portal domain (e.g., https://dev.bionovaq.com)

    Returns:
        {"success": True, "data": {...distributor info...}}
    """
    if not domain:
        return {
            "success": False,
            "error": "domain is required"
        }

    api_url = portal_domain_to_api_url(domain)
    return await make_api_call(
        token="",
        api_url=api_url,
        method="POST",
        endpoint="/api/Distributor/PostByDomain",
        body={"Domain": domain}
    )


@mcp.tool()
async def get_countries(domain: str = "https://dev.bionovaq.com") -> Dict[str, Any]:
    """Fetch complete list of countries available in the system

    Args:
        domain: Portal domain (default: https://dev.bionovaq.com)
    """
    api_url = portal_domain_to_api_url(domain)
    return await make_api_call(
        token="",
        api_url=api_url,
        method="GET",
        endpoint="/api/Country/"
    )


@mcp.tool()
async def get_languages(domain: str = "https://dev.bionovaq.com") -> Dict[str, Any]:
    """Fetch all available system languages for internationalization

    Args:
        domain: Portal domain (default: https://dev.bionovaq.com)
    """
    api_url = portal_domain_to_api_url(domain)
    return await make_api_call(
        token="",
        api_url=api_url,
        method="GET",
        endpoint="/api/Language/GetLanguages"
    )


@mcp.tool()
async def get_user_screen_permissions(session_uuid: str = "") -> Dict[str, Any]:
    """Get user's screen-level access permissions

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/UserScreenPermissions",
        query_params=f"AppUserId={session['app_user_id']}"
    )


@mcp.tool()
async def get_general_object(session_uuid: str = "") -> Dict[str, Any]:
    """Get general configuration objects for logged user

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/GeneralObject/getgeneralobjectjson",
        query_params=f"UserLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_user_language(session_uuid: str = "") -> Dict[str, Any]:
    """Get user's preferred language setting

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/language/GetUserLanguage",
        query_params=f"userLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_regional_format(session_uuid: str = "") -> Dict[str, Any]:
    """Get user's regional format settings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/regionalformat/GetByUser",
        query_params=f"userLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def validate_eula(session_uuid: str = "") -> Dict[str, Any]:
    """Validate if company has accepted the EULA

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint=f"/api/EulaCompanies/validateeulacompany/{session['company_id']}"
    )


# === MENU AND NAVIGATION ENDPOINTS (2) ===

@mcp.tool()
async def get_app_menu(session_uuid: str = "") -> Dict[str, Any]:
    """Get application menu structure based on user permissions

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/menu/getappmenujson",
        query_params=f"UserLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_screen_config(session_uuid: str = "", screen_name_id: str = "") -> Dict[str, Any]:
    """Get screen configuration for specific application screens

    REQUIRES: session_uuid from login()
    """
    if not screen_name_id:
        return {"success": False, "error": "screen_name_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/screen/getjson",
        query_params=f"ScreenNameId={screen_name_id}&UserLoggedId={session['app_user_id']}"
    )


# === SECTOR MANAGEMENT ENDPOINTS (2) ===

@mcp.tool()
async def get_sector(session_uuid: str = "", sector_id: str = "") -> Dict[str, Any]:
    """Get detailed sector or department information by ID

    REQUIRES: session_uuid from login()
    """
    if not sector_id:
        return {"success": False, "error": "sector_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint=f"/api/Sector/{sector_id}"
    )


@mcp.tool()
async def get_sectors_by_company(session_uuid: str = "") -> Dict[str, Any]:
    """Get all sectors for the company

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Sector",
        query_params=f"companyid={session['company_id']}"
    )


# === RECEPTION ENDPOINTS (2) ===

@mcp.tool()
async def get_areas(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of all areas within the company facility

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/area",
        query_params=f"companyId={session['company_id']}"
    )


@mcp.tool()
async def get_pending_returns(session_uuid: str = "", area: str = "") -> Dict[str, Any]:
    """Get list of pending material returns

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    query = f"area={area}" if area else ""
    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/reception/pending-returns",
        query_params=query
    )


# === MATERIALS MANAGEMENT ENDPOINTS (9) ===

@mcp.tool()
async def get_material_types(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of all material type categories

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/MaterialType"
    )


@mcp.tool()
async def get_material_type_list(session_uuid: str = "") -> Dict[str, Any]:
    """Get simple non-paginated list of all material types

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/materialType"
    )


@mcp.tool()
async def get_material_type_paginated(session_uuid: str = "", page: str = "1",
                                     page_size: str = "10", include_count: str = "true") -> Dict[str, Any]:
    """Get paginated list of material types

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/MaterialType/list",
        query_params="&".join(params)
    )


@mcp.tool()
async def get_methods(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of all sterilization methods

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/method"
    )


@mcp.tool()
async def get_method_details(session_uuid: str = "", method_id: str = "") -> Dict[str, Any]:
    """Get detailed information about a specific sterilization method

    REQUIRES: session_uuid from login()
    """
    if not method_id:
        return {"success": False, "error": "method_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint=f"/api/method/{method_id}"
    )


@mcp.tool()
async def get_material_list(session_uuid: str = "", page: str = "1", page_size: str = "10",
                           include_count: str = "true", search: str = "", method: str = "") -> Dict[str, Any]:
    """Get paginated list of materials with filtering

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")
    if search:
        params.append(f"search={search}")
    if method:
        params.append(f"method={method}")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/material/list",
        query_params="&".join(params)
    )


@mcp.tool()
async def get_material_list_simple(session_uuid: str = "") -> Dict[str, Any]:
    """Get simple non-paginated list of all materials

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Material"
    )


@mcp.tool()
async def create_material(session_uuid: str = "", name: str = "", material_type_id: str = "",
                         is_serialized: str = "false", is_implant: str = "false", serial: str = "",
                         method_id: str = "", observations: str = "", cycles_warning: str = "0") -> Dict[str, Any]:
    """Create a new surgical material or instrument

    REQUIRES: session_uuid from login()
    """
    if not name or not material_type_id:
        return {"success": False, "error": "name and material_type_id are required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    body = {
        "id": None,
        "name": name,
        "materialTypeId": material_type_id,
        "isSerialized": is_serialized.lower() == "true",
        "isImplant": is_implant.lower() == "true",
        "serial": serial,
        "methodId": method_id,
        "observations": observations,
        "cyclesWarning": cycles_warning,
        "moveToSaved": False
    }

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/material",
        body=body
    )


@mcp.tool()
async def delete_material(session_uuid: str = "", material_id: str = "") -> Dict[str, Any]:
    """Delete a material by ID

    DESTRUCTIVE OPERATION - REQUIRES EXPLICIT USER CONFIRMATION

    REQUIRES: session_uuid from login()
    """
    if not material_id:
        return {"success": False, "error": "material_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="DELETE",
        endpoint="/api/Material",
        body={"id": int(material_id)}
    )


@mcp.tool()
async def delete_material_type(session_uuid: str = "", material_type_id: str = "") -> Dict[str, Any]:
    """Delete a material type by ID

    DESTRUCTIVE OPERATION - REQUIRES EXPLICIT USER CONFIRMATION

    REQUIRES: session_uuid from login()
    """
    if not material_type_id:
        return {"success": False, "error": "material_type_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="DELETE",
        endpoint="/api/MaterialType",
        body={"id": int(material_type_id)}
    )


@mcp.tool()
async def delete_location(session_uuid: str = "", location_id: str = "") -> Dict[str, Any]:
    """Delete a storage location by ID

    DESTRUCTIVE OPERATION - REQUIRES EXPLICIT USER CONFIRMATION

    REQUIRES: session_uuid from login()
    """
    if not location_id:
        return {"success": False, "error": "location_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="DELETE",
        endpoint="/api/Location",
        body={"id": int(location_id)}
    )


@mcp.tool()
async def delete_brand(session_uuid: str = "", brand_id: str = "") -> Dict[str, Any]:
    """Delete a company machine brand by ID

    DESTRUCTIVE OPERATION - REQUIRES EXPLICIT USER CONFIRMATION

    REQUIRES: session_uuid from login()
    """
    if not brand_id:
        return {"success": False, "error": "brand_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="DELETE",
        endpoint="/api/CompanyMachineMake",
        body={"id": int(brand_id)}
    )


@mcp.tool()
async def delete_model(session_uuid: str = "", model_id: str = "") -> Dict[str, Any]:
    """Delete a company machine model by ID

    DESTRUCTIVE OPERATION - REQUIRES EXPLICIT USER CONFIRMATION

    REQUIRES: session_uuid from login()
    """
    if not model_id:
        return {"success": False, "error": "model_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="DELETE",
        endpoint="/api/CompanyMachineModel",
        body={"id": int(model_id)}
    )


@mcp.tool()
async def delete_package_type(session_uuid: str = "", package_type_id: str = "") -> Dict[str, Any]:
    """Delete a predefined package type by ID

    DESTRUCTIVE OPERATION - REQUIRES EXPLICIT USER CONFIRMATION

    REQUIRES: session_uuid from login()
    """
    if not package_type_id:
        return {"success": False, "error": "package_type_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="DELETE",
        endpoint="/api/PredefinedPackage",
        body={"id": int(package_type_id)}
    )


@mcp.tool()
async def get_material_parameters(session_uuid: str = "") -> Dict[str, Any]:
    """Get material-specific parameters

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/parameter/material"
    )


# === PACKAGES MANAGEMENT ENDPOINTS (4) ===

@mcp.tool()
async def get_package_list(session_uuid: str = "", page: str = "1", page_size: str = "10",
                          include_count: str = "true", date_from: str = "", date_to: str = "",
                          number: str = "", cycle_number: str = "", status: str = "", search: str = "",
                          method: str = "", available_to_move: str = "") -> Dict[str, Any]:
    """Get paginated list of packages with filtering

    REQUIRES: session_uuid from login()
    WORKFLOW: Call after authentication
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")
    if date_from:
        params.append(f"dateFrom={date_from}")
    if date_to:
        params.append(f"dateTo={date_to}")
    if number:
        params.append(f"number={number}")
    if cycle_number:
        params.append(f"cycleNumber={cycle_number}")
    if status:
        params.append(f"status={status}")
    if search:
        params.append(f"search={search}")
    if method:
        params.append(f"method={method}")
    if available_to_move:
        params.append(f"availableToMove={available_to_move}")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Package/list",
        query_params="&".join(params)
    )


@mcp.tool()
async def create_package(session_uuid: str = "", description: str = "", observations: str = "",
                        method_id: str = "1", package_status: str = "1",
                        materials_json: str = "[]") -> Dict[str, Any]:
    """Create a new sterilization package with materials

    REQUIRES: session_uuid from login()

    Args:
        session_uuid: Session UUID from login()
        description: Package description (required)
        observations: Additional observations
        method_id: Sterilization method ID (default: "1")
        package_status: "1" for saved, "2" for finished (default: "1")
        materials_json: JSON array format: [{"MaterialId":1,"Quantity":1}]

    Returns:
        {
            "success": True,
            "data": {"packageId": 12345, "status": 1, "description": "..."}
        }
    """
    if not description:
        return {"success": False, "error": "description is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    try:
        materials = json.loads(materials_json)
    except json.JSONDecodeError:
        return {"success": False, "error": "materials_json must be valid JSON array"}

    body = {
        "Description": description,
        "Observations": observations,
        "MethodId": method_id if method_id else str(1),
        "PackageStatus": int(package_status) if package_status else 1,
        "Materials": materials,
        "Indicators": []
    }

    # Add AreaId from session profile_id (required by API)
    profile_id = session.get("profile_id", "")
    if profile_id:
        body["AreaId"] = int(profile_id)

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/package",
        body=body
    )


@mcp.tool()
async def get_stored_packages(session_uuid: str = "") -> Dict[str, Any]:
    """Get packages organized by storage location

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/package/stored"
    )


@mcp.tool()
async def get_predefined_package_list(session_uuid: str = "", page: str = "1",
                                     page_size: str = "10", include_count: str = "true") -> Dict[str, Any]:
    """Get paginated list of predefined package templates

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/PredefinedPackage/list",
        query_params="&".join(params)
    )


# === WASHING ENDPOINTS (6) ===

@mcp.tool()
async def get_washing_charge_list(session_uuid: str = "", page: str = "1", page_size: str = "10",
                                 include_count: str = "true", number: str = "", status: str = "",
                                 search: str = "", method: str = "", washer: str = "",
                                 date_from: str = "", date_to: str = "") -> Dict[str, Any]:
    """Get paginated list of washing cycles

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")
    if number:
        params.append(f"number={number}")
    if status:
        params.append(f"status={status}")
    if search:
        params.append(f"search={search}")
    if method:
        params.append(f"method={method}")
    if washer:
        params.append(f"washer={washer}")
    if date_from:
        params.append(f"dateFrom={date_from}")
    if date_to:
        params.append(f"dateTo={date_to}")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Chargewashing/list",
        query_params="&".join(params)
    )


@mcp.tool()
async def get_washing_charge_parameters(session_uuid: str = "") -> Dict[str, Any]:
    """Get washing charge-specific parameters

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/parameter/chargeWashing"
    )


@mcp.tool()
async def get_washers_by_sector(session_uuid: str = "", sector_id: str = "") -> Dict[str, Any]:
    """Get list of washers in a specific sector

    REQUIRES: session_uuid from login()
    """
    if not sector_id:
        return {"success": False, "error": "sector_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/washer/getBySector",
        query_params=f"sectorId={sector_id}&companyId={session['company_id']}"
    )


@mcp.tool()
async def get_washers_by_company(session_uuid: str = "") -> Dict[str, Any]:
    """Get all washers for the company

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/washer",
        query_params=f"CompanyId={session['company_id']}"
    )


@mcp.tool()
async def get_washers_by_user(session_uuid: str = "") -> Dict[str, Any]:
    """Get washers accessible to the logged-in user

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/washer/GetByUserLogged"
    )


@mcp.tool()
async def get_package_washing_list(session_uuid: str = "", page: str = "1", page_size: str = "10",
                                  include_count: str = "true", date_from: str = "",
                                  date_to: str = "") -> Dict[str, Any]:
    """Get list of washing package loads

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")
    if date_from:
        params.append(f"dateFrom={date_from}")
    if date_to:
        params.append(f"dateTo={date_to}")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/PackageWashing/list",
        query_params="&".join(params)
    )


# === HYGIENE MONITORING ENDPOINTS (5) ===

@mcp.tool()
async def get_hygiene_list(session_uuid: str = "", page: str = "1", page_size: str = "10",
                          include_count: str = "true", number: str = "", charge_number: str = "",
                          material: str = "", user: str = "", washer_id: str = "", status: str = "",
                          result: str = "", date_from: str = "", date_to: str = "") -> Dict[str, Any]:
    """Get paginated list of hygiene monitoring tests

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")
    if number:
        params.append(f"number={number}")
    if charge_number:
        params.append(f"chargeNumber={charge_number}")
    if material:
        params.append(f"material={material}")
    if user:
        params.append(f"user={user}")
    if washer_id:
        params.append(f"washerId={washer_id}")
    if status:
        params.append(f"status={status}")
    if result:
        params.append(f"result={result}")
    if date_from:
        params.append(f"dateFrom={date_from}")
    if date_to:
        params.append(f"dateTo={date_to}")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/hygiene/list",
        query_params="&".join(params)
    )


@mcp.tool()
async def get_hygiene_parameters(session_uuid: str = "") -> Dict[str, Any]:
    """Get hygiene monitoring parameters

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/parameter/hygiene"
    )


@mcp.tool()
async def get_products_for_hygiene(session_uuid: str = "", method_id: str = "0") -> Dict[str, Any]:
    """Get list of products for hygiene monitoring

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/product/newList",
        query_params=f"methodId={method_id}"
    )


@mcp.tool()
async def get_visual_results_protein(session_uuid: str = "") -> Dict[str, Any]:
    """Get enumeration values for protein test visual results

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Enum/GetVisualResultsPRO"
    )


@mcp.tool()
async def get_possible_results_protein(session_uuid: str = "") -> Dict[str, Any]:
    """Get possible result values for protein test readings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingPRO/GetPossibleResults"
    )


# === CHARGES MANAGEMENT ENDPOINTS (3) ===

@mcp.tool()
async def get_new_charge(session_uuid: str = "") -> Dict[str, Any]:
    """Get information needed to create a new sterilizer charge

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/charge/newcharge"
    )


@mcp.tool()
async def get_charge_list(session_uuid: str = "", page: str = "1", page_size: str = "10",
                         include_count: str = "true", date_from: str = "", date_to: str = "",
                         cycle_number: str = "", status: str = "", search: str = "",
                         method: str = "", sterilizer: str = "") -> Dict[str, Any]:
    """Get paginated list of sterilization charges

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")
    if date_from:
        params.append(f"dateFrom={date_from}")
    if date_to:
        params.append(f"dateTo={date_to}")
    if cycle_number:
        params.append(f"cycleNumber={cycle_number}")
    if status:
        params.append(f"status={status}")
    if search:
        params.append(f"search={search}")
    if method:
        params.append(f"method={method}")
    if sterilizer:
        params.append(f"sterilizer={sterilizer}")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Charge/list",
        query_params="&".join(params)
    )


@mcp.tool()
async def get_charge_parameters(session_uuid: str = "") -> Dict[str, Any]:
    """Get charge-specific parameters

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/parameter/charge"
    )


# === READINGS ENDPOINTS (1) ===

@mcp.tool()
async def get_reading_bi_list(session_uuid: str = "", page: str = "1", page_size: str = "100",
                             include_count: str = "true", position: str = "") -> Dict[str, Any]:
    """Get paginated list of biological indicator readings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")
    if position:
        params.append(f"position={position}")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/readingbi/list",
        query_params="&".join(params)
    )


# === STERILIZATION ENDPOINTS (10) ===

@mcp.tool()
async def get_incubators(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of incubators for biological indicator reading

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Incubator",
        query_params=f"userId={session['app_user_id']}"
    )


@mcp.tool()
async def get_incubators_by_user(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of auto-reader incubators accessible to user

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Incubator",
        query_params=f"appUserId={session['app_user_id']}"
    )


@mcp.tool()
async def get_locations(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of all storage locations

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/location"
    )


@mcp.tool()
async def get_location_list(session_uuid: str = "") -> Dict[str, Any]:
    """Get detailed list of all storage locations

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/location"
    )


@mcp.tool()
async def get_sterilizers_by_sector(session_uuid: str = "", sector_id: str = "") -> Dict[str, Any]:
    """Get list of sterilizers in a specific sector

    REQUIRES: session_uuid from login()
    """
    if not sector_id:
        return {"success": False, "error": "sector_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Sterilizer/getBySector",
        query_params=f"sectorId={sector_id}&companyId={session['company_id']}"
    )


@mcp.tool()
async def get_sterilizers_by_company(session_uuid: str = "") -> Dict[str, Any]:
    """Get all sterilizers for the company

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Sterilizer",
        query_params=f"CompanyId={session['company_id']}"
    )


@mcp.tool()
async def get_sterilizers_by_user(session_uuid: str = "") -> Dict[str, Any]:
    """Get sterilizers accessible to the logged-in user

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Sterilizer/GetByUserLogged"
    )


@mcp.tool()
async def get_sterilizer_list_by_sector(session_uuid: str = "", sector_id: str = "",
                                       page: str = "1", page_size: str = "10",
                                       include_count: str = "true") -> Dict[str, Any]:
    """Get paginated list of sterilizers by sector

    REQUIRES: session_uuid from login()
    """
    if not sector_id:
        return {"success": False, "error": "sector_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [
        f"CompanyId={session['company_id']}",
        f"SectorId={sector_id}",
        f"page={page}",
        f"pageSize={page_size}"
    ]
    if include_count.lower() == "true":
        params.append("includeCount=true")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/sterilizer/listBySector",
        query_params="&".join(params)
    )


@mcp.tool()
async def get_disinfectors_by_company(session_uuid: str = "") -> Dict[str, Any]:
    """Get all disinfectors for the company

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Disinfector",
        query_params=f"CompanyId={session['company_id']}"
    )


@mcp.tool()
async def get_disinfectors_by_user(session_uuid: str = "") -> Dict[str, Any]:
    """Get disinfectors accessible to the logged-in user

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Disinfector/GetByUserLogged"
    )


@mcp.tool()
async def get_color_parameters(session_uuid: str = "") -> Dict[str, Any]:
    """Get color parameter options for location color coding

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/parameter/color"
    )


# === DASHBOARD ENDPOINTS (6) ===

@mcp.tool()
async def get_panel_user(session_uuid: str = "") -> Dict[str, Any]:
    """Get user's dashboard panel preferences

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/PanelUser"
    )


@mcp.tool()
async def get_active_panels(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of active dashboard panels

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Panel/GetActives"
    )


@mcp.tool()
async def get_app_notifications(session_uuid: str = "") -> Dict[str, Any]:
    """Get application notifications for the logged-in user

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/appnotification",
        query_params=f"userLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_bi_reads_by_sector(session_uuid: str = "") -> Dict[str, Any]:
    """Get biological indicator reading results by sector

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingBI/ReadsBySector"
    )


@mcp.tool()
async def get_protein_reads_by_sector(session_uuid: str = "") -> Dict[str, Any]:
    """Get protein test reading results by sector

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingPRO/ReadsBySector"
    )


@mcp.tool()
async def get_bi_reads_by_product(session_uuid: str = "") -> Dict[str, Any]:
    """Get biological indicator reading results by product

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingBI/ReadsByProduct"
    )


# === COMPANY MANAGEMENT ENDPOINTS (2) ===

@mcp.tool()
async def get_company_info(session_uuid: str = "") -> Dict[str, Any]:
    """Get detailed company information

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint=f"/api/company/{session['company_id']}"
    )


@mcp.tool()
async def get_users_by_company(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of all users in the company

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/appuser/getByCompany",
        query_params=f"companyId={session['company_id']}"
    )


# === MACHINE MANAGEMENT ENDPOINTS (5) ===

@mcp.tool()
async def get_machine_brands_by_type(session_uuid: str = "", machine_type_id: str = "1") -> Dict[str, Any]:
    """Get machine brands by type (1=Sterilizer, 2=Washer, 3=Disinfector)

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/machinemake/ByMachineTypeId",
        query_params=f"machineTypeId={machine_type_id}"
    )


@mcp.tool()
async def get_company_machine_make_list(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of all machine brands for the company

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/companyMachineMake"
    )


@mcp.tool()
async def get_company_machine_make_paginated(session_uuid: str = "", page: str = "1",
                                            page_size: str = "10",
                                            include_count: str = "true") -> Dict[str, Any]:
    """Get paginated list of machine brands

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/CompanyMachineMake/list",
        query_params="&".join(params)
    )


@mcp.tool()
async def get_company_machine_model_list(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of all machine models for the company

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/CompanyMachineModel"
    )


@mcp.tool()
async def get_company_machine_model_paginated(session_uuid: str = "", page: str = "1",
                                             page_size: str = "10",
                                             include_count: str = "true") -> Dict[str, Any]:
    """Get paginated list of machine models

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]
    if include_count.lower() == "true":
        params.append("includeCount=true")

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/CompanyMachineModel/list",
        query_params="&".join(params)
    )


# === DISPATCH ENDPOINTS (2) ===

@mcp.tool()
async def get_dispatch_list(session_uuid: str = "", page: str = "1", page_size: str = "10") -> Dict[str, Any]:
    """Get paginated list of package dispatches

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    params = [f"page={page}", f"pageSize={page_size}"]

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/dispatch/list",
        query_params="&".join(params)
    )


@mcp.tool()
async def get_suppliers(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of external suppliers

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/supplier"
    )


# === SERVICE MODULE ENDPOINTS (5) ===

@mcp.tool()
async def get_areas_by_user(session_uuid: str = "") -> Dict[str, Any]:
    """Get areas assigned to user

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/AppUserArea/GetAreasByUserId"
    )


@mcp.tool()
async def get_procedure_codes(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of surgical procedure codes

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/procedurecode"
    )


@mcp.tool()
async def get_surgery_events(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of surgery events

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/surgery"
    )


@mcp.tool()
async def get_programs(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of sterilization or washing programs

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Program"
    )


@mcp.tool()
async def get_sectors_to_assign(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of sectors that can be assigned

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Sector/getSectorsToAssign",
        query_params=f"userId={session['app_user_id']}"
    )


# === REPORTS AND QUERIES ENDPOINTS (32) ===

@mcp.tool()
async def get_bi_process_info(session_uuid: str = "") -> Dict[str, Any]:
    """Get biological indicator process configuration

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingBI/GetProcessBI"
    )


@mcp.tool()
async def get_bi_condition_status(session_uuid: str = "") -> Dict[str, Any]:
    """Get biological indicator condition status

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingBI/GetconditionSCIB",
        query_params=f"userLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_products_by_distributor(session_uuid: str = "", distributor_id: str = "") -> Dict[str, Any]:
    """Get products by distributor

    REQUIRES: session_uuid from login()
    """
    if not distributor_id:
        return {"success": False, "error": "distributor_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Product/GetByDistributorEncrypted",
        query_params=f"distributorId={distributor_id}"
    )


@mcp.tool()
async def get_screen_required_fields(session_uuid: str = "", screen: str = "") -> Dict[str, Any]:
    """Get required fields for a specific screen

    REQUIRES: session_uuid from login()
    """
    if not screen:
        return {"success": False, "error": "screen parameter is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Screen/GetRequiredFields",
        query_params=f"Screen={screen}"
    )


@mcp.tool()
async def get_my_licenses(session_uuid: str = "") -> Dict[str, Any]:
    """Get active licenses for the user

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/LicenseCode/MyLicenses"
    )


@mcp.tool()
async def get_live_bi_readings(session_uuid: str = "") -> Dict[str, Any]:
    """Get live biological indicator readings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingBI/getlive",
        query_params=f"UserLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_completed_bi_readings(session_uuid: str = "") -> Dict[str, Any]:
    """Get completed biological indicator readings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingBI/getprovisional",
        query_params=f"UserLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_htm_chart_models(session_uuid: str = "") -> Dict[str, Any]:
    """Get HTM chart models for IQAS compliance

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/HTM/GetChartModels"
    )


@mcp.tool()
async def get_app_parameters_by_type(session_uuid: str = "", app_parameter_type_id: str = "") -> Dict[str, Any]:
    """Get application parameters by type

    REQUIRES: session_uuid from login()
    """
    if not app_parameter_type_id:
        return {"success": False, "error": "app_parameter_type_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/appparameter/getbytype",
        query_params=f"userLoggedId={session['app_user_id']}&appParameterTypeId={app_parameter_type_id}"
    )


@mcp.tool()
async def get_spr_limits_translations(session_uuid: str = "") -> Dict[str, Any]:
    """Get SPR limits and translations

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/RPE/limitsTranslations"
    )


@mcp.tool()
async def get_visual_results_bi(session_uuid: str = "") -> Dict[str, Any]:
    """Get enumeration values for biological indicator visual results

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Enum/GetVisualResultsBI"
    )


@mcp.tool()
async def get_possible_results_bi(session_uuid: str = "") -> Dict[str, Any]:
    """Get possible result values for biological indicators

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingBI/GetPossibleResults"
    )


@mcp.tool()
async def get_user_evaluation_enum(session_uuid: str = "") -> Dict[str, Any]:
    """Get enumeration values for user evaluation

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Enum/getUserEvaluation"
    )


@mcp.tool()
async def get_completed_sterilizer_chemical_readings(session_uuid: str = "") -> Dict[str, Any]:
    """Get completed sterilizer chemical indicator readings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingCHESterilizer/getprovisional",
        query_params=f"UserLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_detergent_types_enum(session_uuid: str = "") -> Dict[str, Any]:
    """Get enumeration values for detergent types

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/Enum/getDetergentTypes"
    )


@mcp.tool()
async def get_completed_washer_chemical_readings(session_uuid: str = "") -> Dict[str, Any]:
    """Get completed washer chemical indicator readings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingCHEWasher/getprovisional",
        query_params=f"UserLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_live_protein_readings(session_uuid: str = "") -> Dict[str, Any]:
    """Get live protein test readings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingPRO/getlive",
        query_params=f"UserLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_completed_protein_readings(session_uuid: str = "") -> Dict[str, Any]:
    """Get completed protein test readings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingPRO/getprovisional",
        query_params=f"UserLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_possible_results_chemical_sterilizer(session_uuid: str = "") -> Dict[str, Any]:
    """Get possible result values for chemical indicator sterilizer readings

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingCHESterilizer/GetPossibleResults"
    )


@mcp.tool()
async def get_historical_bi_readings(session_uuid: str = "", page: str = "1", page_size: str = "10",
                                    include_count: str = "true", date_from: str = "", date_to: str = "",
                                    sector: str = "", sterilizer: str = "", disinfector: str = "",
                                    result: str = "", visual_result: str = "", cycle_number: str = "",
                                    program: str = "", product: str = "") -> Dict[str, Any]:
    """Get historical biological indicator readings with comprehensive filtering

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    body = {
        "page": int(page),
        "pageSize": int(page_size),
        "includeCount": include_count.lower() == "true"
    }
    if date_from:
        body["dateFrom"] = date_from
    if date_to:
        body["dateTo"] = date_to
    if sector:
        body["sector"] = sector
    if sterilizer:
        body["sterilizer"] = sterilizer
    if disinfector:
        body["disinfector"] = disinfector
    if result:
        body["result"] = result
    if visual_result:
        body["visualResult"] = visual_result
    if cycle_number:
        body["cycleNumber"] = cycle_number
    if program:
        body["program"] = program
    if product:
        body["product"] = product

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/historical/ReadingBI",
        body=body
    )


@mcp.tool()
async def get_incubators_from_readings(session_uuid: str = "", product_type: str = "") -> Dict[str, Any]:
    """Get incubators used in BI readings by product type

    REQUIRES: session_uuid from login()
    """
    if not product_type:
        return {"success": False, "error": "product_type is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingBI/GetIncubatorsFromReadings",
        query_params=f"productType={product_type}"
    )


@mcp.tool()
async def get_historical_bi_disinfection_readings(session_uuid: str = "", page: str = "1",
                                                 page_size: str = "10", include_count: str = "true",
                                                 date_from: str = "", date_to: str = "", sector: str = "",
                                                 disinfector: str = "", result: str = "",
                                                 visual_result: str = "", cycle_number: str = "",
                                                 program: str = "", product: str = "") -> Dict[str, Any]:
    """Get historical BI disinfection readings with filtering

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    body = {
        "page": int(page),
        "pageSize": int(page_size),
        "includeCount": include_count.lower() == "true"
    }
    if date_from:
        body["dateFrom"] = date_from
    if date_to:
        body["dateTo"] = date_to
    if sector:
        body["sector"] = sector
    if disinfector:
        body["disinfector"] = disinfector
    if result:
        body["result"] = result
    if visual_result:
        body["visualResult"] = visual_result
    if cycle_number:
        body["cycleNumber"] = cycle_number
    if program:
        body["program"] = program
    if product:
        body["product"] = product

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/historical/ReadingBIDisinfection",
        body=body
    )


@mcp.tool()
async def get_incubators_from_disinfection_readings(session_uuid: str = "",
                                                   product_type: str = "") -> Dict[str, Any]:
    """Get incubators used in BI disinfection readings

    REQUIRES: session_uuid from login()
    """
    if not product_type:
        return {"success": False, "error": "product_type is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingBIDisinfection/GetIncubatorsFromReadings",
        query_params=f"productType={product_type}"
    )


@mcp.tool()
async def get_historical_protein_readings(session_uuid: str = "", page: str = "1",
                                         page_size: str = "10", include_count: str = "true",
                                         date_from: str = "", date_to: str = "", sector: str = "",
                                         washer: str = "", result: str = "", visual_result: str = "",
                                         cycle_number: str = "", program: str = "", product: str = "",
                                         instruments: str = "", area: str = "") -> Dict[str, Any]:
    """Get historical protein test readings with filtering

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    body = {
        "page": int(page),
        "pageSize": int(page_size),
        "includeCount": include_count.lower() == "true"
    }
    if date_from:
        body["dateFrom"] = date_from
    if date_to:
        body["dateTo"] = date_to
    if sector:
        body["sector"] = sector
    if washer:
        body["washer"] = washer
    if result:
        body["result"] = result
    if visual_result:
        body["visualResult"] = visual_result
    if cycle_number:
        body["cycleNumber"] = cycle_number
    if program:
        body["program"] = program
    if product:
        body["product"] = product
    if instruments:
        body["instruments"] = instruments
    if area:
        body["area"] = area

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/historical/ReadingPRO",
        body=body
    )


@mcp.tool()
async def get_incubators_from_protein_readings(session_uuid: str = "",
                                              product_type: str = "") -> Dict[str, Any]:
    """Get incubators used in protein test readings

    REQUIRES: session_uuid from login()
    """
    if not product_type:
        return {"success": False, "error": "product_type is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ReadingPRO/GetIncubatorsFromReadings",
        query_params=f"productType={product_type}"
    )


@mcp.tool()
async def get_historical_chemical_sterilizer_readings(session_uuid: str = "", page: str = "1",
                                                     page_size: str = "10",
                                                     include_count: str = "true",
                                                     date_from: str = "", date_to: str = "",
                                                     sector: str = "", sterilizer: str = "",
                                                     automatic_reading: str = "",
                                                     user_evaluation: str = "",
                                                     cycle_number: str = "", program: str = "",
                                                     product: str = "") -> Dict[str, Any]:
    """Get historical chemical sterilizer readings with filtering

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    body = {
        "page": int(page),
        "pageSize": int(page_size),
        "includeCount": include_count.lower() == "true"
    }
    if date_from:
        body["dateFrom"] = date_from
    if date_to:
        body["dateTo"] = date_to
    if sector:
        body["sector"] = sector
    if sterilizer:
        body["sterilizer"] = sterilizer
    if automatic_reading:
        body["automaticReading"] = automatic_reading
    if user_evaluation:
        body["userEvaluation"] = user_evaluation
    if cycle_number:
        body["cycleNumber"] = cycle_number
    if program:
        body["program"] = program
    if product:
        body["product"] = product

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/historical/readingchesterilizer",
        body=body
    )


@mcp.tool()
async def get_historical_chemical_washer_readings(session_uuid: str = "", page: str = "1",
                                                 page_size: str = "10",
                                                 include_count: str = "true",
                                                 date_from: str = "", date_to: str = "",
                                                 sector: str = "", washer: str = "",
                                                 automatic_reading: str = "",
                                                 user_evaluation: str = "",
                                                 cycle_number: str = "", program: str = "",
                                                 product: str = "") -> Dict[str, Any]:
    """Get historical chemical washer readings with filtering

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    body = {
        "page": int(page),
        "pageSize": int(page_size),
        "includeCount": include_count.lower() == "true"
    }
    if date_from:
        body["dateFrom"] = date_from
    if date_to:
        body["dateTo"] = date_to
    if sector:
        body["sector"] = sector
    if washer:
        body["washer"] = washer
    if automatic_reading:
        body["automaticReading"] = automatic_reading
    if user_evaluation:
        body["userEvaluation"] = user_evaluation
    if cycle_number:
        body["cycleNumber"] = cycle_number
    if program:
        body["program"] = program
    if product:
        body["product"] = product

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/historical/readingchewasher",
        body=body
    )


# === SYSTEM CONFIGURATION ENDPOINTS (3) ===

@mcp.tool()
async def get_app_log(session_uuid: str = "") -> Dict[str, Any]:
    """Get application event log entries

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/applog",
        query_params=f"userLoggedId={session['app_user_id']}"
    )


@mcp.tool()
async def get_profile_list(session_uuid: str = "") -> Dict[str, Any]:
    """Get list of user profiles or roles

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/Api/Profile/list"
    )


@mcp.tool()
async def get_screen_permissions(session_uuid: str = "") -> Dict[str, Any]:
    """Get available screen permissions

    REQUIRES: session_uuid from login()
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/ScreenPermissions"
    )


# === NEW ENDPOINTS v2.1 - WORKFLOW COMPLETION ===

@mcp.tool()
async def create_material_type(
    session_uuid: str = "",
    name: str = "",
    description: str = "",
    observations: str = ""
) -> Dict[str, Any]:
    """Create a new material type category

    REQUIRES: session_uuid from login()

    Args:
        name: Material type name (required, must be unique)
        description: Material type description (required)
        observations: Additional observations (optional)

    Endpoint: POST /api/MaterialType
    Payload Example: {
        "name": "MaterialTest",
        "description": "Material De Prueba",
        "observations": "Ninguna"
    }

    Returns:
        {
            "success": True,
            "data": {"materialTypeId": 173, "name": "MaterialTest", "description": "Material De Prueba"}
        }
    """
    if not name or not description:
        return {"success": False, "error": "name and description are required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    body = {
        "name": name,
        "description": description,
        "observations": observations
    }

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/MaterialType",
        body=body
    )


@mcp.tool()
async def add_material_stock(
    session_uuid: str = "",
    materials_json: str = "[]"
) -> Dict[str, Any]:
    """Add stock quantity to one or multiple materials

    REQUIRES: session_uuid from login()

    Args:
        materials_json: JSON array format: [{"materialId": 9548, "quantity": 13}]

    Endpoint: POST /api/materialStock

    Initial Status: DIRTY (statusId=1)

    Returns:
        {
            "success": True,
            "materials_updated": 1,
            "total_quantity_added": 13
        }
    """
    if not materials_json or materials_json == "[]":
        return {"success": False, "error": "materials_json is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    try:
        materials = json.loads(materials_json)
        if not isinstance(materials, list):
            return {"success": False, "error": "materials_json must be a JSON array"}
    except json.JSONDecodeError:
        return {"success": False, "error": "materials_json must be valid JSON array"}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/materialStock",
        body=materials
    )


@mcp.tool()
async def modify_material_stock_status(
    session_uuid: str = "",
    material_id: str = "",
    status_from: str = "",
    status_to: str = "",
    quantity_to_move: str = ""
) -> Dict[str, Any]:
    """Change material stock status (e.g., Dirty → Available for conditioning)

    REQUIRES: session_uuid from login()

    Args:
        material_id: Material ID
        status_from: Source status ID (e.g., "1" for Dirty)
        status_to: Target status ID (e.g., "4" for Available for conditioning)
        quantity_to_move: Quantity to move

    Status IDs Reference:
        1 = Dirty
        2 = Washing
        3 = Washed
        4 = Available for conditioning
        5 = In package
        6 = In sterilization Load
        7 = Sterilized without releasing
        8 = Sterilized and released
        9 = Stored
        10 = In Area
        11 = Free
        12 = Pre-verified
        13 = Not Available (deprecated)
        14 = Missing
        15 = Single Use (deprecated)
        16 = Broken
        17 = Post-verified
        18 = Implanted
        19 = Single Use
        20 = Broken In Transit To CSSD (deprecated)
        21 = In Transit To CSSD

    Endpoint: PUT /api/materialStock/modifyStock
    Payload: [{
        "materialStockId": material_id,
        "StatusFrom": status_from,
        "statusTo": status_to,
        "QuantityToMove": quantity_to_move
    }]

    Returns:
        {
            "success": True,
            "material_id": "9548",
            "quantity_moved": 13,
            "from_status": 1,
            "to_status": 4
        }
    """
    if not all([material_id, status_from, status_to, quantity_to_move]):
        return {"success": False, "error": "material_id, status_from, status_to, and quantity_to_move are required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    body = [{
        "materialStockId": int(material_id),
        "StatusFrom": int(status_from),
        "statusTo": int(status_to),
        "QuantityToMove": int(quantity_to_move)
    }]

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="PUT",
        endpoint="/api/materialStock/modifyStock",
        body=body
    )


@mcp.tool()
async def get_material_stock_by_status(
    session_uuid: str = ""
) -> Dict[str, Any]:
    """Get material stock grouped by status

    REQUIRES: session_uuid from login()

    Args:
        session_uuid: Session UUID from login()

    Endpoint: GET /api/materialStock/listByStatus?CompanyId={CompanyId}

    Returns:
        {
            "success": True,
            "data": [
                {
                    "statusMaterialId": 1,
                    "statusMaterial": "Dirty",
                    "totalQuantity": 4937,
                    "quantityByMaterial": [
                        {"name": "Material1", "quantity": 10, "serialNumber": null},
                        ...
                    ]
                },
                ...
            ]
        }
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/materialStock/listByStatus",
        query_params=f"CompanyId={session['company_id']}"
    )


@mcp.tool()
async def get_material_stock_by_material(
    session_uuid: str = ""
) -> Dict[str, Any]:
    """Get stock status breakdown for each material

    REQUIRES: session_uuid from login()

    Args:
        session_uuid: Session UUID from login()

    Endpoint: GET /api/materialStock/ListByMaterialStatus?CompanyId={CompanyId}

    Returns:
        {
            "success": True,
            "data": [
                {
                    "materialId": 9548,
                    "materialName": "Manual_Test_Package",
                    "materialSerialNumber": "",
                    "totalStatusses": 13,
                    "statusesByMaterial": [
                        {"status": "Dirty", "statusId": 1, "quantity": 13},
                        {"status": "Washing", "statusId": 2, "quantity": 0},
                        ...
                    ]
                },
                ...
            ]
        }
    """
    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/materialStock/ListByMaterialStatus",
        query_params=f"CompanyId={session['company_id']}"
    )


@mcp.tool()
async def update_package(
    session_uuid: str = "",
    package_id: str = "",
    description: str = "",
    observations: str = "",
    method_id: str = "1",
    package_status: str = "1",
    materials_json: str = "[]",
    indicators_json: str = "[]",
    predefined_package_id: str = ""
) -> Dict[str, Any]:
    """Update existing package (save or finish)

    REQUIRES: session_uuid from login()

    Args:
        package_id: Package ID (required)
        description: Package description
        observations: Additional observations
        method_id: Sterilization method ID
        package_status: "1" for saved, "2" for finished
        materials_json: JSON array: [{"materialId": 9316, "quantity": 2}]
        indicators_json: JSON array: [{"indicatorId": 18, "quantity": 1}]
        predefined_package_id: ID of predefined package template (optional)

    Endpoint: PUT /api/package
    Payload: {
        "id": 20443,
        "description": "TestPKGBiologicos",
        "materials": [{"materialId": 9316, "quantity": 2}],
        "indicators": [],
        "methodId": 1,
        "observations": "",
        "packageStatus": 1,
        "predefinedPackageId": null
    }

    Returns:
        {
            "success": True,
            "packageId": 20443,
            "status": 2,
            "materialsInPackage": 10
        }
    """
    if not package_id:
        return {"success": False, "error": "package_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    try:
        materials = json.loads(materials_json) if materials_json else []
        indicators = json.loads(indicators_json) if indicators_json else []
    except json.JSONDecodeError:
        return {"success": False, "error": "materials_json or indicators_json must be valid JSON"}

    body = {
        "id": int(package_id),
        "description": description,
        "observations": observations,
        "methodId": int(method_id) if method_id else 1,
        "packageStatus": int(package_status) if package_status else 1,
        "materials": materials,
        "indicators": indicators
    }

    if predefined_package_id:
        body["predefinedPackageId"] = int(predefined_package_id)
    else:
        body["predefinedPackageId"] = None

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="PUT",
        endpoint="/api/package",
        body=body
    )


@mcp.tool()
async def create_charge(
    session_uuid: str = "",
    sterilizer_id: str = "",
    used_program: str = "",
    cycle_number: str = "",
    charge_status: str = "1",
    observations: str = "",
    packages_json: str = "[]",
    indicators_json: str = "[]"
) -> Dict[str, Any]:
    """Create a new sterilization charge

    REQUIRES: session_uuid from login()

    Args:
        sterilizer_id: Sterilizer ID (required)
        used_program: Program name (e.g., "Standard")
        cycle_number: Cycle number (auto-incremented if not provided)
        charge_status: "1" for saved, "2" for finished
        observations: Additional observations
        packages_json: JSON array: [{"PackageId": 20443}]
        indicators_json: JSON array: [{
            "IndicatorId": 18,
            "quantity": 1,
            "serial": null,
            "lot": "B40380",
            "modelAreasId": 55
        }]

    Endpoint: POST /api/charge
    Payload: {
        "sterilizerId": "1123",
        "usedProgram": "Standard",
        "chargeStatus": 1,
        "indicators": [...],
        "packages": [{"PackageId": 20443}],
        "observations": "",
        "cycleNumber": 18
    }

    Returns:
        {
            "success": True,
            "chargeId": 3804,
            "cycleNumber": 18,
            "status": 1
        }
    """
    if not sterilizer_id:
        return {"success": False, "error": "sterilizer_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    try:
        packages = json.loads(packages_json) if packages_json else []
        indicators = json.loads(indicators_json) if indicators_json else []
    except json.JSONDecodeError:
        return {"success": False, "error": "packages_json or indicators_json must be valid JSON"}

    body = {
        "sterilizerId": str(sterilizer_id),
        "usedProgram": used_program,
        "chargeStatus": int(charge_status) if charge_status else 1,
        "indicators": indicators,
        "packages": packages,
        "observations": observations
    }

    if cycle_number:
        body["cycleNumber"] = int(cycle_number)

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="POST",
        endpoint="/api/charge",
        body=body
    )


@mcp.tool()
async def update_charge(
    session_uuid: str = "",
    charge_id: str = "",
    sterilizer_id: str = "",
    used_program: str = "",
    cycle_number: str = "",
    charge_status: str = "1",
    observations: str = "",
    packages_json: str = "[]",
    indicators_json: str = "[]"
) -> Dict[str, Any]:
    """Update existing charge (save or finish)

    REQUIRES: session_uuid from login()

    Args:
        charge_id: Charge ID (required)
        sterilizer_id: Sterilizer ID (required)
        used_program: Program name
        cycle_number: Cycle number
        charge_status: "1" for saved, "2" for finished
        observations: Additional observations
        packages_json: JSON array: [{"PackageId": 20443}]
        indicators_json: JSON array (same format as create_charge)

    Endpoint: PUT /api/charge
    Payload: Same as create_charge but includes "id"

    Returns:
        {
            "success": True,
            "chargeId": 3804,
            "status": 2,
            "packagesInLoad": 1
        }
    """
    if not charge_id:
        return {"success": False, "error": "charge_id is required"}

    if not sterilizer_id:
        return {"success": False, "error": "sterilizer_id is required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    try:
        packages = json.loads(packages_json) if packages_json else []
        indicators = json.loads(indicators_json) if indicators_json else []
    except json.JSONDecodeError:
        return {"success": False, "error": "packages_json or indicators_json must be valid JSON"}

    body = {
        "id": int(charge_id),
        "sterilizerId": str(sterilizer_id),
        "usedProgram": used_program,
        "chargeStatus": int(charge_status) if charge_status else 1,
        "indicators": indicators,
        "packages": packages,
        "observations": observations
    }

    if cycle_number:
        body["cycleNumber"] = int(cycle_number)

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="PUT",
        endpoint="/api/charge",
        body=body
    )


@mcp.tool()
async def verify_charge_rules(
    session_uuid: str = "",
    indicators_json: str = "[]",
    packages_json: str = "[]",
    method_id: str = "",
    time: str = "",
    sterilizer_id: str = ""
) -> Dict[str, Any]:
    """Verify charge rules before finishing

    REQUIRES: session_uuid from login()

    Args:
        indicators_json: JSON array of indicator IDs: [18, 19]
        packages_json: JSON array of package IDs: [20443, 20444]
        method_id: Sterilization method ID
        time: Time in format "HH:MM"
        sterilizer_id: Sterilizer ID

    Endpoint: PUT /api/rule/verify
    Payload: {
        "indicators": [18],
        "packages": [20443],
        "methodId": 1,
        "time": "14:50",
        "sterilizerId": "1123"
    }

    Returns:
        {
            "success": True,
            "valid": true,
            "warnings": [],
            "errors": []
        }
    """
    if not all([indicators_json, packages_json, method_id, sterilizer_id]):
        return {"success": False, "error": "indicators_json, packages_json, method_id, and sterilizer_id are required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    try:
        indicators = json.loads(indicators_json) if indicators_json else []
        packages = json.loads(packages_json) if packages_json else []
    except json.JSONDecodeError:
        return {"success": False, "error": "indicators_json or packages_json must be valid JSON"}

    body = {
        "indicators": indicators,
        "packages": packages,
        "methodId": int(method_id),
        "time": time,
        "sterilizerId": str(sterilizer_id)
    }

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="PUT",
        endpoint="/api/rule/verify",
        body=body
    )


@mcp.tool()
async def get_indicator_lot_for_charge(
    session_uuid: str = "",
    product: str = "",
    lot: str = "",
    dist_id: str = "",
    method_id: str = ""
) -> Dict[str, Any]:
    """Get indicator lot information for charge

    REQUIRES: session_uuid from login()

    Args:
        product: Indicator product name (e.g., "BD125X/2")
        lot: Lot number (e.g., "B40380")
        dist_id: Distributor ID
        method_id: Sterilization method ID

    Endpoint: GET /api/IndicatorLot/lotForCharge
    Query: Product=BD125X/2&lot=B40380&distId=2&methodId=1

    Returns:
        {
            "success": True,
            "data": {
                "valid": true,
                "indicatorId": 18,
                "productName": "BD125X/2",
                "lot": "B40380",
                "expirationDate": "2026-01-15",
                "modelAreasId": 55
            }
        }
    """
    if not all([product, lot, method_id]):
        return {"success": False, "error": "product, lot, and method_id are required"}

    session = session_manager.load_session(session_uuid)
    if not session:
        return {"success": False, "error": "Invalid or expired session. Please login first."}

    query_params = f"Product={product}&lot={lot}&methodId={method_id}"
    if dist_id:
        query_params += f"&distId={dist_id}"

    return await make_api_call(
        token=session["token"],
        api_url=session["api_url"],
        method="GET",
        endpoint="/api/IndicatorLot/lotForCharge",
        query_params=query_params
    )


# === SERVER STARTUP ===

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("BionovaQ MCP Server v2.1 - Session-based Architecture")
    logger.info("=" * 60)
    logger.info(f"Sessions directory: {SESSIONS_DIR}")
    logger.info("Total tools: 125 (114 original + 11 new workflow endpoints)")
    logger.info("")
    logger.info("WORKFLOW:")
    logger.info("1. Call login() with credentials -> receives session_uuid")
    logger.info("2. Pass session_uuid to all subsequent tool calls")
    logger.info("3. Call logout() to end session")
    logger.info("")
    logger.info("Multi-user support: Each user has independent session state")
    logger.info("=" * 60)

    try:
        mcp.run(transport='stdio')
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)
