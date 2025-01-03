from fastapi import Request
import logging
import asyncio
from app.schemas import QueryResponse, FileInfo, TruncatedSandboxResult
import os

logger = logging.getLogger(__name__)

# Add client disconnection check
async def check_client_connection(request: Request) -> bool:
    """Enhanced connection check with retry logic"""
    MAX_RETRIES = 3
    RETRY_DELAY = 1  # seconds
    
    for attempt in range(MAX_RETRIES):
        try:
            if await request.is_disconnected():
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                logger.warning("Client disconnected after retries")
                return False
            return True
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
                continue
            logger.error(f"Connection check error: {e}")
            return False
    
    return True



def construct_status_response(job: dict) -> QueryResponse:
    """Helper function to construct status response"""

    current_status = job.get("status", "unknown")

    logger.info(f"Current chunk: {job.get('current_chunk')}, Total chunks: {len(job.get('page_chunks', []))}")
    
    if current_status == "completed":
        if job["output_preferences"]["type"] == "online":
            return QueryResponse(
                result=TruncatedSandboxResult(
                    original_query=job["query"],
                    print_output="",
                    error=None,
                    timed_out=False,
                    return_value_snapshot=job["result_snapshot"]
                ),
                status=current_status,
                message="Processing completed",
                files=None,
                num_images_processed=job["total_images_processed"],
                total_pages=job.get("total_pages", 0)
            )
        else:  # download type
            return QueryResponse(
                result=TruncatedSandboxResult(
                    original_query=job["query"],
                    print_output="",
                    error=None,
                    timed_out=False,
                    return_value_snapshot=None
                ),
                status=current_status,
                message="File ready for download",
                files=[FileInfo(
                    file_path=job["result_file_path"],
                    media_type=job["result_media_type"],
                    filename=os.path.basename(job["result_file_path"]),
                    download_url=f"/download?file_path={job['result_file_path']}"
                )],
                num_images_processed=job["total_images_processed"],
                total_pages=job.get("total_pages", 0)
            )
    
    elif current_status == "error":
        return QueryResponse(
            result=None,
            status="error",
            message=job["error_message"],
            files=None,
            num_images_processed=job["total_images_processed"],
            total_pages=job.get("total_pages", 0)
        )
    
    else:  # processing or created
        message = job.get("message", "Processing in progress")
        
        if job.get('page_chunks'):
            page_chunks = job.get('page_chunks', [])
            current_chunk = int(job.get('current_chunk', 0))
            file_id = page_chunks[current_chunk]['file_id']
            start_page = int(page_chunks[current_chunk]['page_range'][0])
            output_preferences = job.get('output_preferences')
            if output_preferences.get('doc_name'):
                doc_name = output_preferences.get('doc_name')
                sheet_name = output_preferences.get('sheet_name')
            else:
                doc_name = None
                sheet_name = None
            total_pages = page_chunks[current_chunk]['metadata']['page_count']
            if output_preferences['type'] == 'online':
                if output_preferences['modify_existing']:
                    message = f"{max(0, start_page)} of {total_pages} pages from file {file_id} processed and appended to {doc_name} - {sheet_name}."
                else:
                    message = f"{max(0, start_page)} of {total_pages} pages from file {file_id} processed and added to new sheet in {doc_name}."
            else: #download output
                message = f"{max(0, start_page)} of {total_pages} pages from file {file_id} processed."
            
        return QueryResponse(
            result=None,
            status=current_status,
            message=message,
            files=None,
            num_images_processed=job['total_images_processed'],
            total_pages=job.get("total_pages", 0)
        )