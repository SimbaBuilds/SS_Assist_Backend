from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Response
from typing import List, Optional, Any, Union
from pydantic import BaseModel
from app.utils.file_preprocessing import FilePreprocessor
from app.utils.process_query import process_query
from app.utils.sandbox import EnhancedPythonInterpreter
from app.schemas import FileDataInfo, QueryResponse, FileInfo, FileMetadata, TruncatedSandboxResult
import json
from app.utils.file_management import temp_file_manager
from app.utils.vision_processing import VisionProcessor
import os
import logging
from app.schemas import QueryRequest
from fastapi.responses import FileResponse
import pandas as pd
from app.utils.document_integrations import DocumentIntegrations
from app.utils.file_postprocessing import create_pdf, create_xlsx, create_docx, create_txt, create_csv
from fastapi import BackgroundTasks
import io
from typing import Dict
router = APIRouter()
def sanitize_error_message(e: Exception) -> str:
    """Sanitize error messages to remove binary data"""
    return str(e).encode('ascii', 'ignore').decode('ascii')

def _create_return_value_snapshot(self) -> str:
    """Creates a string representation of the return value"""
    try:
        if isinstance(self.return_value, tuple):
            return ', '.join(str(item) for item in self.return_value)
        return str(self.return_value)
    except Exception:
        return "<unprintable value>"

def get_data_snapshot(content: Any, data_type: str) -> str:
    """Generate appropriate snapshot based on data type"""
    if data_type == "DataFrame":
        return content.head(10).to_string()
    elif data_type == "json":
        # For JSON, return first few key-value pairs or array elements
        if isinstance(content, dict):
            snapshot_dict = dict(list(content.items())[:5])
            return json.dumps(snapshot_dict, indent=2)
        elif isinstance(content, list):
            return json.dumps(content[:5], indent=2)
        return str(content)[:500]
    elif data_type == "text":
        # Return first 500 characters for text
        return content[:500] + ("..." if len(content) > 500 else "")
    elif data_type == "image":
        content.file.seek(0)
        size = len(content.file.read())
        content.file.seek(0)
        return f"Image file: {content.filename}, Size: {size} bytes"
    return str(content)[:500]

async def preprocess_files(
    files: List[UploadFile],
    files_metadata: List[FileMetadata],
    web_urls: List[str],
    query: str,
    session_dir
) -> List[FileDataInfo]:
    """Helper function to preprocess files and web URLs"""
    preprocessor = FilePreprocessor()
    processed_data = []
    
    # Process web URLs if provided
    for url in web_urls:
        try:
            logging.info(f"Processing URL: {url}")
            content = preprocessor.preprocess_file(url, 'web_url')
            data_type = "DataFrame" if isinstance(content, pd.DataFrame) else "text"
            processed_data.append(
                FileDataInfo(
                    content=content,
                    snapshot=get_data_snapshot(content, data_type),
                    data_type=data_type,
                    original_file_name=url.split('/')[-1],
                    url=url
                )
            )
        except Exception as e:
            error_msg = sanitize_error_message(e)
            logging.error(f"Error processing URL {url}: {error_msg}")
            raise Exception(f"Error processing URL {url}: {error_msg}")
    
    # Process uploaded files using metadata
    if files and files_metadata:
        for metadata in files_metadata:
            try:
                file = files[metadata.index]
                logging.info(f"Preprocessing file: {metadata.name} with type: {metadata.type}")
                
                # Map MIME types to FilePreprocessor types
                mime_to_processor = {
                    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xlsx',
                    'text/csv': 'csv',
                    'application/json': 'json',
                    'text/plain': 'txt',
                    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
                    'image/png': 'png',
                    'image/jpeg': 'jpg',
                    'image/jpg': 'jpg',
                    'application/pdf': 'pdf'
                }
                
                file_type = mime_to_processor.get(metadata.type)
                if not file_type:
                    raise ValueError(f"Unsupported MIME type: {metadata.type}")

                # Handle special cases for images and PDFs that need additional parameters
                kwargs = {}
                if file_type in ['png', 'jpg', 'jpeg']:
                    kwargs['output_path'] = str(session_dir / f"{metadata.name}.jpeg")
                elif file_type == 'pdf':
                    kwargs['query'] = query

                # Process the file
                with io.BytesIO(file.file.read()) as file_obj:
                    file_obj.seek(0)  # Reset the file pointer to the beginning
                    content = preprocessor.preprocess_file(file_obj, file_type, **kwargs)

                # Handle different return types
                if file_type == 'pdf':
                    content, data_type, is_readable = content  # Unpack PDF processor return values
                    metadata_info = {"is_readable": is_readable}
                else:
                    data_type = "DataFrame" if isinstance(content, pd.DataFrame) else "text"
                    metadata_info = {}

                processed_data.append(
                    FileDataInfo(
                        content=content,
                        snapshot=get_data_snapshot(content, data_type),
                        data_type=data_type,
                        original_file_name=metadata.name,
                        metadata=metadata_info,
                        new_file_path=kwargs.get('output_path')  # For images
                    )
                )

            except Exception as e:
                error_msg = sanitize_error_message(e)
                logging.error(f"Error processing file {metadata.name}: {error_msg}")
                raise ValueError(f"Error processing file {metadata.name}: {error_msg}")

    return processed_data

async def handle_destination_upload(data: Any, destination_url: str) -> bool:
    """Upload data to various destination types"""
    try:
        doc_integrations = DocumentIntegrations()
        url_lower = destination_url.lower()
        
        if "docs.google.com" in url_lower:
            if "document" in url_lower:
                return await doc_integrations.append_to_google_doc(data, destination_url)
            elif "spreadsheets" in url_lower:
                return await doc_integrations.append_to_google_sheet(data, destination_url)
        
        elif "onedrive" in url_lower or "sharepoint.com" in url_lower:
            if "docx" in url_lower:
                return await doc_integrations.append_to_office_doc(data, destination_url)
            elif "xlsx" in url_lower:
                return await doc_integrations.append_to_office_sheet(data, destination_url)
        
        raise ValueError(f"Unsupported destination URL type: {destination_url}")
    
    except Exception as e:
        logging.error(f"Failed to upload to destination: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@router.post("/process_query", response_model=QueryResponse)
async def process_query_endpoint(
    json_data: str = Form(...),
    files: List[UploadFile] = File(None),
    background_tasks: BackgroundTasks = BackgroundTasks()
) -> QueryResponse:
    try:
        request = QueryRequest(**json.loads(json_data))
        print
        logging.info(f"Processing query with {len(request.files_metadata or [])} files")
        session_dir = temp_file_manager.get_temp_dir()
        print("Calling preprocess_files")
        try:
            preprocessed_data = await preprocess_files(
                files=files,
                files_metadata=request.files_metadata,
                web_urls=request.web_urls,
                query=request.query,
                session_dir=session_dir
            )
        except Exception as e:
            try:
                error_msg = str(e)
                if not error_msg.isascii() or len(error_msg) > 200:
                    error_msg = f"Error processing files: {e.__class__.__name__}"
                else:
                    error_msg = error_msg.encode('ascii', 'ignore').decode('ascii')
            except:
                error_msg = "Error processing files"
                
            logging.error(f"File preprocessing error: {e.__class__.__name__}")
            raise ValueError(error_msg)

        # Process the query with the processed data
        sandbox = EnhancedPythonInterpreter()
        result = process_query(
            query=request.query,
            sandbox=sandbox,
            data=preprocessed_data
        )
        result.return_value_snapshot = _create_return_value_snapshot(result.return_value)
        print("Query processed with return value snapshot:", result.return_value_snapshot, "and error:", result.error)
        print("Output preferences type:", request.output_preferences.type, "and format:", request.output_preferences.format)
        # Handle output based on type
        if request.output_preferences.type == "download":
            # Get the desired output format, defaulting based on data type
            output_format = request.output_preferences.format
            if not output_format:
                if isinstance(result.return_value, pd.DataFrame):
                    output_format = 'csv'
                elif isinstance(result.return_value, (dict, list)):
                    output_format = 'json'
                else:
                    output_format = 'txt'

            # Create temporary file in requested format
            if output_format == 'pdf':
                tmp_path = create_pdf(result.return_value)
                media_type = 'application/pdf'
            elif output_format == 'xlsx':
                tmp_path = create_xlsx(result.return_value)
                media_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            elif output_format == 'docx':
                tmp_path = create_docx(result.return_value)
                media_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            elif output_format == 'txt':
                tmp_path = create_txt(result.return_value)
                media_type = 'text/plain'
            else:  # csv
                tmp_path = create_csv(result.return_value)
                media_type = 'text/csv'

            # Add cleanup task but DON'T execute immediately
            background_tasks.add_task(temp_file_manager.cleanup_marked)
            
            # Update download URL to match client expectations
            download_url = f"/download?file_path={tmp_path}"
            
            truncated_result = TruncatedSandboxResult(
                original_query=result.original_query,
                print_output=result.print_output,
                error=result.error,
                timed_out=result.timed_out,
                return_value_snapshot=result.return_value_snapshot
            )
            return QueryResponse(
                result=truncated_result,
                status="success",
                message="File ready for download",
                files=[FileInfo(
                    file_path=str(tmp_path),
                    media_type=media_type,
                    filename=os.path.basename(tmp_path),
                    download_url=download_url  # Updated download URL format
                )]
            )

        elif request.output_preferences.type == "online":
            if not request.output_preferences.destination_url:
                raise ValueError("destination_url is required for online type")
                
            # Handle destination URL upload
            await handle_destination_upload(
                result.return_value,
                request.output_preferences.destination_url
            )

            # Only cleanup immediately for online type
            temp_file_manager.cleanup_marked()

            return QueryResponse(
                result=truncated_result,
                status="success",
                message="Data successfully uploaded to destination",
                files=None
            )
        
        else:
            raise ValueError(f"Invalid output type: {request.output_preferences.type}")


    except Exception as e:
        # Enhanced error handling for binary content
        try:
            error_msg = str(e)
            if not error_msg.isascii() or len(error_msg) > 200:
                error_msg = f"Error processing request: {e.__class__.__name__}"
            else:
                error_msg = error_msg.encode('ascii', 'ignore').decode('ascii')
        except:
            error_msg = "An unexpected error occurred"
            
        logging.error(f"Process query error: {e.__class__.__name__}")
        return QueryResponse(
            result = truncated_result,
            status="error",
            message= "an error occurred while processing your request -- please try again",
            files=None
        )

@router.get("/download")
async def download_file(
    file_path: str,
    background_tasks: BackgroundTasks
) -> FileResponse:
    """
    Serve a processed file for download
    
    Args:
        file_path: Full path to the file to download
        background_tasks: FastAPI background tasks handler
    """
    try:
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="File not found")
            
        # Determine the media type based on file extension
        filename = os.path.basename(file_path)
        extension = filename.split('.')[-1].lower()
        media_types = {
            'pdf': 'application/pdf',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'txt': 'text/plain',
            'csv': 'text/csv'
        }
        
        media_type = media_types.get(extension, 'application/octet-stream')
        
        # Add cleanup task to run after file is sent
        background_tasks.add_task(temp_file_manager.cleanup_marked)
        
        # Return the file as a response
        return FileResponse(
            path=file_path,
            media_type=media_type,
            filename=filename
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error serving file: {str(e)}")
