from app.schemas import FileDataInfo
from typing import List, Tuple
from app.utils.llm import file_namer
import logging
from fastapi import HTTPException
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from msgraph import GraphServiceClient
import pandas as pd
import os
from datetime import datetime, timedelta, timezone, date
import aiohttp
import requests
from typing import Any
from supabase.client import Client as SupabaseClient



class MicrosoftIntegration:
    def __init__(self, supabase: SupabaseClient, user_id: str):
        self._one_drive_id = None

        ms_response = supabase.table('user_documents_access') \
        .select('refresh_token') \
        .match({'user_id': user_id, 'provider': 'microsoft'}) \
        .execute()
        
        if not ms_response.data or len(ms_response.data) == 0:
            print(f"No Microsoft token found for user {user_id}")
            return None
        ms_refresh_token = ms_response.data[0]['refresh_token']

        # Microsoft auth setup
        if ms_refresh_token:
            self.ms_refresh_token = ms_refresh_token
            self._refresh_microsoft_token()
            self.ms_client = GraphServiceClient(
                credentials=self.ms_access_token,
                scopes=['https://graph.microsoft.com/.default']
            )

    def _refresh_microsoft_token(self) -> None:
        """Refresh Microsoft OAuth token"""
        try:
            data = {
                'client_id': os.getenv('MS_CLIENT_ID'),
                'client_secret': os.getenv('MS_CLIENT_SECRET'),
                'refresh_token': self.ms_refresh_token,
                'grant_type': 'refresh_token',
                'scope': 'https://graph.microsoft.com/.default'
            }
            
            response = requests.post(
                'https://login.microsoftonline.com/common/oauth2/v2.0/token',
                data=data
            )
            response.raise_for_status()
            token_data = response.json()
            
            # Store the token data
            self.ms_access_token = token_data['access_token']
            self.ms_refresh_token = token_data.get('refresh_token', self.ms_refresh_token)
            self.ms_token_type = token_data['token_type']
            self.ms_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(token_data['expires_in']))
            
            # Update the Graph client with new credentials
            self.ms_client = GraphServiceClient(
                credentials=self.ms_access_token,
                scopes=['https://graph.microsoft.com/.default']
            )
            
        except Exception as e:
            logging.error(f"Failed to refresh Microsoft token: {str(e)}")
            raise HTTPException(status_code=401, detail="Failed to refresh Microsoft token")

    async def _ensure_valid_microsoft_token(self) -> None:
        """Check and refresh Microsoft token if expired"""
        if datetime.now(timezone.utc) >= self.ms_expires_at:
            self._refresh_microsoft_token()

    async def _get_microsoft_headers(self) -> dict:
        """Get valid Microsoft API headers with current token"""
        await self._ensure_valid_microsoft_token()
        return {
            'Authorization': f"{self.ms_token_type} {self.ms_access_token}",
            'Content-Type': 'application/json'
        }

    def _format_data_for_excel(self, data: Any) -> List[List[Any]]:
        """Helper method to format data for Excel"""
        def format_value(v: Any) -> str:
            """Helper function to format individual values"""
            if isinstance(v, pd.Timestamp):
                return v.strftime('%Y-%m-%d %H:%M:%S')
            elif isinstance(v, (datetime, date)):
                return v.strftime('%Y-%m-%d')
            elif pd.isna(v):
                return ''
            else:
                return str(v)

        if isinstance(data, pd.DataFrame):
            df_copy = data.copy()
            # Handle datetime columns
            for col in df_copy.select_dtypes(include=['datetime64[ns]']).columns:
                df_copy[col] = df_copy[col].dt.strftime('%Y-%m-%d %H:%M:%S')
            # Replace NaN values with empty string
            df_copy = df_copy.fillna('')
            # Convert any remaining date objects
            for col in df_copy.columns:
                if df_copy[col].dtype == 'object':
                    df_copy[col] = df_copy[col].apply(format_value)
            return [df_copy.columns.tolist()] + df_copy.values.tolist()
        elif isinstance(data, (dict, list)):
            if isinstance(data, dict):
                processed_dict = {k: format_value(v) for k, v in data.items()}
                return [[k, v] for k, v in processed_dict.items()]
            else:
                return [[format_value(v)] for v in data]
        return [[format_value(data)]]

    async def _get_one_drive_id(self) -> str:
        """Get the drive ID using the Graph API for personal OneDrive"""
        if not self._one_drive_id:
            headers = await self._get_microsoft_headers()
            async with aiohttp.ClientSession() as session:
                async with session.get('https://graph.microsoft.com/v1.0/me/drive', headers=headers) as response:
                    if response.status == 200:
                        drive_info = await response.json()
                        self._one_drive_id = drive_info['id']
                    else:
                        response_text = await response.text()
                        error_data = await response.json()
                        error_code = error_data.get('error', {}).get('code', 'Unknown')
                        if error_code == "InvalidAuthenticationToken":
                            raise HTTPException(status_code=401, detail="Invalid or expired Microsoft access token")
                        elif error_code == "ResourceNotFound":
                            raise HTTPException(status_code=404, detail="OneDrive not found for this account")
                        raise HTTPException(status_code=response.status, detail=f"OneDrive access failed: {error_code} - {response_text}")
        return self._one_drive_id

    async def _get_one_drive_and_item_info(self, file_url: str) -> Tuple[str, str]:
        """Get drive ID and item ID from a OneDrive URL"""
        drive_id = await self._get_one_drive_id()
        if 'id=' not in file_url:
            raise ValueError("Could not find item ID in OneDrive URL")
        item_id = file_url.split('id=')[1].split('&')[0]
        if not item_id:
            raise ValueError("Empty item ID extracted from OneDrive URL")
        return drive_id, item_id

    async def _manage_office_session(self, item_id: str, action: str = 'create') -> str:
        """Manage Microsoft Office Excel sessions"""
        headers = await self._get_microsoft_headers()
        base_url = f'https://graph.microsoft.com/v1.0/me/drive/items/{item_id}/workbook'
        
        if action == 'create':
            async with aiohttp.ClientSession() as session:
                async with session.post(f'{base_url}/createSession', headers=headers, json={"persistChanges": True}) as response:
                    if response.status == 201:
                        response_data = await response.json()
                        return response_data['id']
                    response_text = await response.text()
                    raise HTTPException(status_code=response.status, detail=f"Failed to create Office session: {response_text}")
        elif action == 'list':
            async with aiohttp.ClientSession() as session:
                async with session.get(f'{base_url}/sessions', headers=headers) as response:
                    if response.status == 200:
                        response_data = await response.json()
                        return response_data.get('value', [])
                    return []
        elif action == 'close' and item_id:
            async with aiohttp.ClientSession() as session:
                async with session.delete(f'{base_url}/sessions/{item_id}', headers=headers) as response:
                    return response.status in [200, 204]
        return None

    async def append_to_current_office_sheet(self, data: Any, sheet_url: str) -> bool:
        """Append data to Office Excel Online workbook"""
        try:
            # Get drive ID and workbook ID
            drive_id, workbook_id = await self._get_one_drive_and_item_info(sheet_url)
            
            headers = await self._get_microsoft_headers()
            values = self._format_data_for_excel(data)
            
            # Get the first worksheet
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f'https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{workbook_id}/workbook/worksheets',
                    headers=headers
                ) as response:
                    if response.status != 200:
                        error_data = await response.json()
                        raise HTTPException(
                            status_code=response.status,
                            detail=f"Failed to get worksheets: {error_data.get('error', {}).get('message', 'Unknown error')}"
                        )
                    worksheets = await response.json()
                    active_sheet = worksheets['value'][0]
                
                # Get the used range to find next empty row
                async with session.get(
                    f'https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{workbook_id}/workbook/worksheets/{active_sheet["id"]}/usedRange',
                    headers=headers
                ) as response:
                    if response.status != 200:
                        error_data = await response.json()
                        raise HTTPException(
                            status_code=response.status,
                            detail=f"Failed to get used range: {error_data.get('error', {}).get('message', 'Unknown error')}"
                        )
                    used_range = await response.json()
                    next_row = used_range['rowCount'] + 1
                
                # Write data to worksheet
                range_address = f"A{next_row}:${chr(65 + len(values[0]) - 1)}${next_row + len(values) - 1}"
                request_body = {
                    'values': values
                }
                
                async with session.patch(
                    f'https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{workbook_id}/workbook/worksheets/{active_sheet["id"]}/range(address=\'{range_address}\')',
                    headers=headers,
                    json=request_body
                ) as response:
                    if response.status not in [200, 204]:
                        error_data = await response.json()
                        raise HTTPException(
                            status_code=response.status,
                            detail=f"Failed to update range: {error_data.get('error', {}).get('message', 'Unknown error')}"
                        )
                
                return True
            
        except Exception as e:
            logging.error(f"Office Excel append error: {str(e)}")
            raise

    async def append_to_new_office_sheet(self, data: Any, sheet_url: str, old_data: List[FileDataInfo], query: str) -> bool:
        """Add data to a new sheet within an existing Office Excel workbook"""
        try:
            # Get drive ID and workbook ID
            drive_id, workbook_id = await self._get_one_drive_and_item_info(sheet_url)
            
            headers = await self._get_microsoft_headers()
            values = self._format_data_for_excel(data)
            
            async with aiohttp.ClientSession() as session:
                # Get existing worksheets
                async with session.get(
                    f'https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{workbook_id}/workbook/worksheets',
                    headers=headers
                ) as response:
                    if response.status != 200:
                        error_data = await response.json()
                        raise HTTPException(
                            status_code=response.status,
                            detail=f"Failed to get worksheets: {error_data.get('error', {}).get('message', 'Unknown error')}"
                        )
                    worksheets = await response.json()
                    existing_sheets = [ws['name'] for ws in worksheets['value']]
                
                # Generate unique sheet name
                base_name = file_namer(query, old_data)
                sheet_name = base_name
                counter = 1
                while sheet_name in existing_sheets:
                    sheet_name = f"{base_name} {counter}"
                    counter += 1
                
                # Create new worksheet
                async with session.post(
                    f'https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{workbook_id}/workbook/worksheets/add',
                    headers=headers,
                    json={'name': sheet_name}
                ) as response:
                    if response.status != 201:
                        error_data = await response.json()
                        raise HTTPException(
                            status_code=response.status,
                            detail=f"Failed to create worksheet: {error_data.get('error', {}).get('message', 'Unknown error')}"
                        )
                    new_worksheet = await response.json()
                
                # Write data to new worksheet
                range_address = f"A1:${chr(65 + len(values[0]) - 1)}${len(values)}"
                request_body = {
                    'values': values
                }
                
                async with session.patch(
                    f'https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{workbook_id}/workbook/worksheets/{new_worksheet["id"]}/range(address=\'{range_address}\')',
                    headers=headers,
                    json=request_body
                ) as response:
                    if response.status not in [200, 204]:
                        error_data = await response.json()
                        raise HTTPException(
                            status_code=response.status,
                            detail=f"Failed to update range: {error_data.get('error', {}).get('message', 'Unknown error')}"
                        )
                
                return True
            
        except Exception as e:
            logging.error(f"New Office Excel worksheet creation error: {str(e)}")
            raise