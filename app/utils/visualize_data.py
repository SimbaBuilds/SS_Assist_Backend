from app.schemas import SandboxResult, FileDataInfo
from app.utils.llm_service import LLMService
from app.utils.data_processing import get_data_snapshot
from app.utils.sandbox import EnhancedPythonInterpreter
from app.utils.process_query import extract_code
import pandas as pd
import logging
import matplotlib.pyplot as plt
from typing import List, Optional, Tuple, Any, Union
from io import BytesIO

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)



async def generate_visualization(
    data: List[FileDataInfo],
    color_palette: str,
    custom_instructions: Optional[str],
    sandbox: EnhancedPythonInterpreter,
    llm_service: LLMService
) -> BytesIO:
    """Generate visualization using the sandbox environment."""
    
    try:
        # Create execution namespace
        namespace = dict(sandbox.base_namespace)
        
        # Add data to namespace
        for idx, file_data in enumerate(data):
            var_name = f'data_{idx}' if idx > 0 else 'data'
            namespace[var_name] = file_data.content
            
            if isinstance(file_data.content, pd.DataFrame):
                logger.info(f"{var_name} shape: {file_data.content.shape}")

        # Get data snapshot for LLM context
        data_snapshot = get_data_snapshot(data[0].content, "DataFrame")
        
        # Set matplotlib backend to Agg for non-interactive plotting
        namespace['plt'].switch_backend('Agg')
        
        # Generate visualization code
        provider, suggested_code = await llm_service.execute_with_fallback(
            "gen_visualization",
            data_snapshot=data_snapshot,
            color_palette=color_palette,
            custom_instructions=custom_instructions
        )
        
        # Clean and prepare the code
        cleaned_code = extract_code(suggested_code)
        logger.info("Generated visualization code:\n%s", cleaned_code)
        
        # Execute the visualization code
        result = sandbox.execute_code(
            original_query="Generate visualization",
            code=cleaned_code,
            namespace=namespace
        )
        
        if result.error:
            logger.error("Error executing visualization code: %s", result.error)
            raise ValueError(f"Failed to create visualization: {result.error}")
        
        # Verify that a plot exists
        if not namespace['plt'].get_fignums():
            raise ValueError("No plot was generated by the code")
            
        # Capture the plot from the namespace's plt object
        buf = BytesIO()
        namespace['plt'].savefig(buf, format='png', bbox_inches='tight', dpi=300)
        buf.seek(0)
        namespace['plt'].close()
        
        return buf
        
    except Exception as e:
        logger.error("Visualization generation failed: %s", str(e))
        try:
            namespace['plt'].close()  # Try to close plot using namespace plt
        except:
            plt.close()  # Fallback to global plt
        raise ValueError(f"Failed to generate visualization: {str(e)}")