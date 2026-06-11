import os
import logging
from abc import ABC, abstractmethod
from typing import List, Union
from openai import AzureOpenAI, OpenAI, AsyncAzureOpenAI, AsyncOpenAI
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(str(Path(__file__).parent / ".env"))

logger = logging.getLogger(__name__)


class AIClient(ABC):
    """Abstract base class for AI clients."""
    
    @abstractmethod
    def get_model_response(self, body: Union[List, str], temperature: float = None) -> str:
        """
        Get response from the AI model.
        
        Args:
            body: Request body (format depends on client implementation)
            temperature: Optional temperature override for this request
            
        Returns:
            str: Model response text
        """
        pass
    
    @abstractmethod
    def get_client_info(self) -> dict:
        """
        Get information about the client.
        
        Returns:
            dict: Client information including model name, provider, etc.
        """
        pass


class GPT41Client(AIClient):
    """Azure OpenAI client for GPT-4.1 model."""
    
    def __init__(self, temperature: float = 0.4):
        self.client = AzureOpenAI(
            azure_endpoint=os.getenv('AZURE_OPENAI_ENDPOINT'),
            api_key=os.getenv('AZURE_OPENAI_API_KEY'),
            api_version=os.getenv('OPENAI_API_VERSION')
        )
        self.model = "gpt-4.1"
        self.temperature = temperature

    def get_model_response(self, body: List, temperature: float = None) -> str:
        """
        Get response from GPT-4.1 model.
        
        Args:
            body: List of messages in OpenAI chat format
            temperature: Optional temperature override for this request
            
        Returns:
            str: Model response text
        """
        logger.info("Invoking Azure OpenAI GPT-4.1 model...")
        temp = temperature if temperature is not None else self.temperature
        response = self.client.chat.completions.create(
            model=self.model,
            messages=body,
            temperature=temp
        )
        return response.choices[0].message.content
    
    def get_client_info(self) -> dict:
        """
        Get information about the GPT-4.1 client.
        
        Returns:
            dict: Client information including model name, provider, etc.
        """
        return {
            "provider": "Azure OpenAI",
            "model": self.model,
            "temperature": self.temperature,
            "endpoint": os.getenv('AZURE_OPENAI_ENDPOINT', 'Not configured')
        }


class InternVL3Client(AIClient):
    """
    Client for InternVL3-78B model served via LMDeploy api_server.
    
    Usage:
        1. Start the LMDeploy server:
           lmdeploy serve api_server OpenGVLab/InternVL3-78B --chat-template internvl2_5 --server-port 23333
        
        2. Create the client:
           client = InternVL3Client(base_url="http://0.0.0.0:23333/v1", api_key="your-key")
    """
    
    def __init__(
        self, 
        base_url: str = None,
        api_key: str = None,
        temperature: float = 0.4
    ):
        """
        Initialize InternVL3 client.
        
        Args:
            base_url: LMDeploy server URL (default: from INTERNVL3_BASE_URL env or http://0.0.0.0:23333/v1)
            api_key: API key for the server (default: from INTERNVL3_API_KEY env or 'not-needed')
            temperature: Default temperature for generation
        """
        self.base_url = base_url or os.getenv('INTERNVL3_BASE_URL', 'http://0.0.0.0:23333/v1')
        self.api_key = api_key or os.getenv('INTERNVL3_API_KEY', 'not-needed')
        self.temperature = temperature
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
        
        # Get model name from server
        self.model = self._get_model_name()
    
    def _get_model_name(self) -> str:
        """Fetch the model name from the LMDeploy server."""
        try:
            models = self.client.models.list()
            return models.data[0].id
        except Exception as e:
            logger.warning(f"Could not fetch model name from server: {e}. Using default.")
            return "OpenGVLab/InternVL3-78B"
    
    def get_model_response(self, body: List, temperature: float = None) -> str:
        """
        Get response from InternVL3 model.
        
        Args:
            body: List of messages in OpenAI chat format. Supports multimodal content:
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe this image"},
                            {"type": "image_url", "image_url": {"url": "https://...image.jpg"}}
                        ]
                    }
                ]
            temperature: Optional temperature override for this request
            
        Returns:
            str: Model response text
        """
        logger.info("Invoking InternVL3-78B model via LMDeploy...")
        temp = temperature if temperature is not None else self.temperature
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=body,
            temperature=temp
        )
        return response.choices[0].message.content
    
    def get_model_response_with_images(
        self, 
        prompt: str, 
        image_urls: List[str], 
        temperature: float = None
    ) -> str:
        """
        Convenience method for sending a prompt with multiple images.
        
        Args:
            prompt: Text prompt
            image_urls: List of image URLs (can be http URLs or base64 data URIs)
            temperature: Optional temperature override
            
        Returns:
            str: Model response text
        """
        content = [{"type": "text", "text": prompt}]
        
        for url in image_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url}
            })
        
        messages = [{"role": "user", "content": content}]
        return self.get_model_response(messages, temperature)
    
    def get_client_info(self) -> dict:
        """
        Get information about the InternVL3 client.
        
        Returns:
            dict: Client information including model name, provider, etc.
        """
        return {
            "provider": "LMDeploy (InternVL3)",
            "model": self.model,
            "temperature": self.temperature,
            "base_url": self.base_url
        }


# =============================================================================
# Async Clients (for use with asyncio)
# =============================================================================

class AsyncAIClient(ABC):
    """Abstract base class for async AI clients."""
    
    @abstractmethod
    async def get_model_response(self, body: Union[List, str], temperature: float = None) -> str:
        """
        Get response from the AI model asynchronously.
        
        Args:
            body: Request body (format depends on client implementation)
            temperature: Optional temperature override for this request
            
        Returns:
            str: Model response text
        """
        pass
    
    @abstractmethod
    def get_client_info(self) -> dict:
        """
        Get information about the client.
        
        Returns:
            dict: Client information including model name, provider, etc.
        """
        pass


class AsyncGPT41Client(AsyncAIClient):
    """Async Azure OpenAI client for GPT-4.1 model."""
    
    def __init__(self, temperature: float = 0.4):
        self.client = AsyncAzureOpenAI(
            azure_endpoint=os.getenv('AZURE_OPENAI_ENDPOINT'),
            api_key=os.getenv('AZURE_OPENAI_API_KEY'),
            api_version=os.getenv('OPENAI_API_VERSION')
        )
        self.model = "gpt-4.1"
        self.temperature = temperature

    async def get_model_response(self, body: List, temperature: float = None) -> str:
        """
        Get response from GPT-4.1 model asynchronously.
        
        Args:
            body: List of messages in OpenAI chat format
            temperature: Optional temperature override for this request
            
        Returns:
            str: Model response text
        """
        logger.info("Invoking Azure OpenAI GPT-4.1 model (async)...")
        temp = temperature if temperature is not None else self.temperature
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=body,
            temperature=temp
        )
        return response.choices[0].message.content
    
    def get_client_info(self) -> dict:
        """
        Get information about the GPT-4.1 client.
        
        Returns:
            dict: Client information including model name, provider, etc.
        """
        return {
            "provider": "Azure OpenAI",
            "model": self.model,
            "temperature": self.temperature,
            "endpoint": os.getenv('AZURE_OPENAI_ENDPOINT', 'Not configured')
        }


class AsyncInternVL3Client(AsyncAIClient):
    """
    Async client for InternVL3-78B model served via LMDeploy api_server.
    
    Usage:
        1. Start the LMDeploy server:
           lmdeploy serve api_server OpenGVLab/InternVL3-78B --chat-template internvl2_5 --server-port 23333
        
        2. Create the client:
           client = AsyncInternVL3Client(base_url="http://0.0.0.0:23333/v1", api_key="your-key")
    """
    
    def __init__(
        self, 
        base_url: str = None,
        api_key: str = None,
        temperature: float = 0.4
    ):
        """
        Initialize async InternVL3 client.
        
        Args:
            base_url: LMDeploy server URL (default: from INTERNVL3_BASE_URL env or http://0.0.0.0:23333/v1)
            api_key: API key for the server (default: from INTERNVL3_API_KEY env or 'not-needed')
            temperature: Default temperature for generation
        """
        self.base_url = base_url or os.getenv('INTERNVL3_BASE_URL')
        self.api_key = api_key or os.getenv('INTERNVL3_API_KEY', 'not-needed')
        self.temperature = temperature
        
        self.async_client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
        
        # Use sync client just to get model name at init
        self._sync_client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
        self.model = self._get_model_name()
    
    def _get_model_name(self) -> str:
        """Fetch the model name from the LMDeploy server."""
        try:
            models = self._sync_client.models.list()
            return models.data[0].id
        except Exception as e:
            logger.warning(f"Could not fetch model name from server: {e}. Using default.")
            return "OpenGVLab/InternVL3-78B"
    
    async def get_model_response(self, body: List, temperature: float = None) -> str:
        """
        Get response from InternVL3 model asynchronously.
        
        Args:
            body: List of messages in OpenAI chat format. Supports multimodal content.
            temperature: Optional temperature override for this request
            
        Returns:
            str: Model response text
        """
        logger.info("Invoking InternVL3-78B model via LMDeploy (async)...")
        temp = temperature if temperature is not None else self.temperature
        
        response = await self.async_client.chat.completions.create(
            model=self.model,
            messages=body,
            temperature=temp
        )
        return response.choices[0].message.content
    
    async def get_model_response_with_images(
        self, 
        prompt: str, 
        image_urls: List[str], 
        temperature: float = None
    ) -> str:
        """
        Convenience method for sending a prompt with multiple images asynchronously.
        
        Args:
            prompt: Text prompt
            image_urls: List of image URLs (can be http URLs or base64 data URIs)
            temperature: Optional temperature override
            
        Returns:
            str: Model response text
        """
        content = [{"type": "text", "text": prompt}]
        
        for url in image_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url}
            })
        
        messages = [{"role": "user", "content": content}]
        return await self.get_model_response(messages, temperature)
    
    def get_client_info(self) -> dict:
        """
        Get information about the InternVL3 client.
        
        Returns:
            dict: Client information including model name, provider, etc.
        """
        return {
            "provider": "LMDeploy (InternVL3)",
            "model": self.model,
            "temperature": self.temperature,
            "base_url": self.base_url
        }


# =============================================================================
# Factory Functions
# =============================================================================

def create_ai_client(provider: str = "gpt41", **kwargs) -> AIClient:
    """
    Factory function to create sync AI clients.
    
    Args:
        provider: Client provider name ("gpt41" or "internvl3")
        **kwargs: Additional arguments for client initialization
        
    Returns:
        AIClient: Configured AI client instance
    """
    if provider.lower() == "gpt41":
        return GPT41Client(**kwargs)
    elif provider.lower() == "internvl3":
        return InternVL3Client(**kwargs)
    else:
        raise ValueError(f"Unknown provider: {provider}. Supported: 'gpt41', 'internvl3'")


def create_async_ai_client(provider: str = "gpt41", **kwargs) -> AsyncAIClient:
    """
    Factory function to create async AI clients.
    
    Args:
        provider: Client provider name ("gpt41" or "internvl3")
        **kwargs: Additional arguments for client initialization
        
    Returns:
        AsyncAIClient: Configured async AI client instance
    """
    if provider.lower() == "gpt41":
        return AsyncGPT41Client(**kwargs)
    elif provider.lower() == "internvl3":
        return AsyncInternVL3Client(**kwargs)
    else:
        raise ValueError(f"Unknown provider: {provider}. Supported: 'gpt41', 'internvl3'")

