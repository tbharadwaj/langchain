from __future__ import annotations

import asyncio
import logging
import socket
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import urlsplit

import requests
from pydantic import BaseSettings, Field, root_validator
from requests import Response

from langchain.base_language import BaseLanguageModel
from langchain.callbacks.manager import tracing_v2_enabled
from langchain.callbacks.tracers.langchain import LangChainTracerV2
from langchain.chains.base import Chain
from langchain.chat_models.base import BaseChatModel
from langchain.client.models import Dataset, Example
from langchain.client.utils import parse_chat_messages
from langchain.llms.base import BaseLLM
from langchain.schema import ChatResult, LLMResult
from langchain.utils import raise_for_status_with_text, xor_args

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)


def _get_link_stem(url: str) -> str:
    scheme = urlsplit(url).scheme
    netloc_prefix = urlsplit(url).netloc.split(":")[0]
    return f"{scheme}://{netloc_prefix}"


def _is_localhost(url: str) -> bool:
    """Check if the URL is localhost."""
    try:
        netloc = urlsplit(url).netloc.split(":")[0]
        ip = socket.gethostbyname(netloc)
        return ip == "127.0.0.1" or ip.startswith("0.0.0.0") or ip.startswith("::")
    except socket.gaierror:
        return False


class LangChainPlusClient(BaseSettings):
    """Client for interacting with the LangChain+ API."""

    api_key: Optional[str] = Field(default=None, env="LANGCHAIN_API_KEY")
    api_url: str = Field(..., env="LANGCHAIN_ENDPOINT")
    tenant_id: str = Field(..., env="LANGCHAIN_TENANT_ID")

    @root_validator(pre=True)
    def validate_api_key_if_hosted(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Verify API key is provided if url not localhost."""
        api_url: str = values.get("api_url", "http://localhost:8000")
        api_key: Optional[str] = values.get("api_key")
        if not _is_localhost(api_url):
            if not api_key:
                raise ValueError(
                    "API key must be provided when using hosted LangChain+ API"
                )
        else:
            tenant_id = values.get("tenant_id")
            if not tenant_id:
                values["tenant_id"] = LangChainPlusClient._get_seeded_tenant_id(
                    api_url, api_key
                )
        return values

    @staticmethod
    def _get_seeded_tenant_id(api_url: str, api_key: Optional[str]) -> str:
        """Get the tenant ID from the seeded tenant."""
        url = f"{api_url}/tenants"
        headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        response = requests.get(url, headers=headers)
        try:
            raise_for_status_with_text(response)
        except Exception as e:
            raise ValueError(
                "Unable to get seeded tenant ID. Please manually provide."
            ) from e
        results: List[dict] = response.json()
        if len(results) == 0:
            raise ValueError("No seeded tenant found")
        return results[0]["id"]

    def _repr_html_(self) -> str:
        """Return an HTML representation of the instance with a link to the URL."""
        link = _get_link_stem(self.api_url)
        return f'<a href="{link}", target="_blank" rel="noopener">LangChain+ Client</a>'

    def __repr__(self) -> str:
        """Return a string representation of the instance with a link to the URL."""
        return f"LangChainPlusClient (API URL: {self.api_url})"

    @property
    def _headers(self) -> Dict[str, str]:
        """Get the headers for the API request."""
        headers = {}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    @property
    def query_params(self) -> Dict[str, str]:
        """Get the headers for the API request."""
        return {"tenant_id": self.tenant_id}

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Response:
        """Make a GET request."""
        query_params = self.query_params
        if params:
            query_params.update(params)
        return requests.get(
            f"{self.api_url}{path}", headers=self._headers, params=query_params
        )

    def upload_dataframe(
        self,
        df: pd.DataFrame,
        name: str,
        description: str,
        input_keys: List[str],
        output_keys: List[str],
    ) -> Dataset:
        """Upload a dataframe as a CSV to the LangChain+ API."""
        if not name.endswith(".csv"):
            raise ValueError("Name must end with .csv")
        if not all([key in df.columns for key in input_keys]):
            raise ValueError("Input keys must be in dataframe columns")
        if not all([key in df.columns for key in output_keys]):
            raise ValueError("Output keys must be in dataframe columns")
        buffer = BytesIO()
        df.to_csv(buffer, index=False)
        buffer.seek(0)
        csv_file = (name, buffer)
        return self.upload_csv(
            csv_file, description, input_keys=input_keys, output_keys=output_keys
        )

    def upload_csv(
        self,
        csv_file: Union[str, Tuple[str, BytesIO]],
        description: str,
        input_keys: List[str],
        output_keys: List[str],
    ) -> Dataset:
        """Upload a CSV file to the LangChain+ API."""
        files = {"file": csv_file}
        data = {
            "input_keys": ",".join(input_keys),
            "output_keys": ",".join(output_keys),
            "description": description,
            "tenant_id": self.tenant_id,
        }
        response = requests.post(
            self.api_url + "/datasets/upload",
            headers=self._headers,
            data=data,
            files=files,
        )
        raise_for_status_with_text(response)
        result = response.json()
        # TODO: Make this more robust server-side
        if "detail" in result and "already exists" in result["detail"]:
            file_name = csv_file if isinstance(csv_file, str) else csv_file[0]
            file_name = file_name.split("/")[-1]
            raise ValueError(f"Dataset {file_name} already exists")
        return Dataset(**result)

    @xor_args(("dataset_name", "dataset_id"))
    def read_dataset(
        self, *, dataset_name: Optional[str] = None, dataset_id: Optional[str] = None
    ) -> Dataset:
        path = "/datasets"
        params: Dict[str, Any] = {"limit": 1, "tenant_id": self.tenant_id}
        if dataset_id is not None:
            path += f"/{dataset_id}"
        elif dataset_name is not None:
            params["name"] = dataset_name
        else:
            raise ValueError("Must provide dataset_name or dataset_id")
        response = self._get(
            path,
            params=params,
        )
        raise_for_status_with_text(response)
        result = response.json()
        if isinstance(result, list):
            if len(result) == 0:
                raise ValueError(f"Dataset {dataset_name} not found")
            return Dataset(**result[0])
        return Dataset(**result)

    def list_datasets(self, limit: int = 100) -> Iterable[Dataset]:
        """List the datasets on the LangChain+ API."""
        response = self._get("/datasets", params={"limit": limit})
        raise_for_status_with_text(response)
        return [Dataset(**dataset) for dataset in response.json()]

    @xor_args(("dataset_id", "dataset_name"))
    def delete_dataset(
        self, *, dataset_id: Optional[str] = None, dataset_name: Optional[str] = None
    ) -> Dataset:
        """Delete a dataset by ID or name."""
        if dataset_name is not None:
            dataset_id = self.read_dataset(dataset_name=dataset_name).id
        if dataset_id is None:
            raise ValueError("Must provide either dataset name or ID")
        response = requests.delete(
            f"{self.api_url}/datasets/{dataset_id}",
            headers=self._headers,
        )
        raise_for_status_with_text(response)
        return response.json()

    def read_example(self, example_id: str) -> Example:
        """Read an example from the LangChain+ API."""
        response = self._get(f"/examples/{example_id}")
        raise_for_status_with_text(response)
        return Example(**response.json())

    def list_examples(self, dataset_id: Optional[str] = None) -> Iterable[Example]:
        """List the datasets on the LangChain+ API."""
        params = {"dataset": dataset_id} if dataset_id is not None else {}
        response = self._get("/examples", params=params)
        raise_for_status_with_text(response)
        return [Example(**dataset) for dataset in response.json()]

    @staticmethod
    async def _arun_llm(
        llm: BaseLanguageModel,
        inputs: Dict[str, Any],
        langchain_tracer: LangChainTracerV2,
    ) -> Union[LLMResult, ChatResult]:
        if isinstance(llm, BaseLLM):
            llm_prompts: List[str] = inputs["prompts"]
            llm_output = await llm.agenerate(llm_prompts, callbacks=[langchain_tracer])
        elif isinstance(llm, BaseChatModel):
            chat_prompts: List[str] = inputs["prompts"]
            messages = [
                parse_chat_messages(chat_prompt) for chat_prompt in chat_prompts
            ]
            llm_output = await llm.agenerate(messages, callbacks=[langchain_tracer])
        else:
            raise ValueError(f"Unsupported LLM type {type(llm)}")
        return llm_output

    @staticmethod
    async def _arun_llm_or_chain(
        example: Example,
        langchain_tracer: LangChainTracerV2,
        llm_or_chain: Union[Chain, BaseLanguageModel],
        n_repetitions: int,
    ) -> Union[List[dict], List[str], List[LLMResult], List[ChatResult]]:
        """Run the chain asynchronously."""
        previous_example_id = langchain_tracer.example_id
        langchain_tracer.example_id = example.id
        outputs = []
        for _ in range(n_repetitions):
            try:
                if isinstance(llm_or_chain, BaseLanguageModel):
                    output: Any = await LangChainPlusClient._arun_llm(
                        llm_or_chain, example.inputs, langchain_tracer
                    )
                else:
                    output = await llm_or_chain.arun(
                        example.inputs, callbacks=[langchain_tracer]
                    )
                outputs.append(output)
            except Exception as e:
                logger.warning(f"Chain failed for example {example.id}. Error: {e}")
                outputs.append({"Error": str(e)})
            finally:
                langchain_tracer.example_id = previous_example_id
        return outputs

    @staticmethod
    async def _worker(
        queue: asyncio.Queue,
        tracer: LangChainTracerV2,
        chain: Chain,
        n_repetitions: int,
        results: Dict[str, Any],
        job_state: Dict[str, Any],
        verbose: bool,
    ) -> None:
        """Worker for running the chain on examples."""
        while True:
            example: Optional[Example] = await queue.get()
            if example is None:
                break

            result = await LangChainPlusClient._arun_llm_or_chain(
                example,
                tracer,
                chain,
                n_repetitions,
            )
            results[str(example.id)] = result
            queue.task_done()
            job_state["num_processed"] += 1
            if verbose:
                print(
                    f"Completed {job_state['num_processed']}",
                    end="\r",
                    flush=True,
                )

    async def arun_on_dataset(
        self,
        dataset_name: str,
        llm_or_chain: Union[Chain, BaseLanguageModel],
        num_workers: int = 5,
        num_repetitions: int = 1,
        session_name: Optional[str] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """Run the chain on a dataset and store traces to the specified session name.

        Args:
            dataset_name: Name of the dataset to run the chain on.
            llm_or_chain: Chain or language model to run over the dataset.
            num_workers: Number of async workers to run in parallel.
            num_repetitions: Number of times to run the chain on each example.
            session_name: Name of the session to store the traces in.
            verbose: Whether to print progress.

        Returns:
            A dictionary mapping example ids to the chain outputs.
        """
        if isinstance(llm_or_chain, BaseLanguageModel):
            raise NotImplementedError
        if session_name is None:
            session_name = (
                f"{dataset_name}_{llm_or_chain.__class__.__name__}-{num_repetitions}"
            )
        dataset = self.read_dataset(dataset_name=dataset_name)
        workers = []
        examples = self.list_examples(dataset_id=str(dataset.id))
        results: Dict[str, Any] = {}
        queue: asyncio.Queue[Optional[Example]] = asyncio.Queue()
        job_state = {"num_processed": 0}
        with tracing_v2_enabled(session_name=session_name) as session:
            for _ in range(num_workers):
                tracer = LangChainTracerV2()
                tracer.session = session
                task = asyncio.create_task(
                    LangChainPlusClient._worker(
                        queue,
                        tracer,
                        llm_or_chain,
                        num_repetitions,
                        results,
                        job_state,
                        verbose,
                    )
                )
                workers.append(task)

            for example in examples:
                await queue.put(example)

            await queue.join()  # Wait for all tasks to complete

            for _ in workers:
                await queue.put(None)  # Signal the workers to exit

            await asyncio.gather(*workers)

            return results

    @staticmethod
    def run_llm(
        llm: BaseLanguageModel,
        inputs: Dict[str, Any],
        langchain_tracer: LangChainTracerV2,
    ) -> Union[LLMResult, ChatResult]:
        if isinstance(llm, BaseLLM):
            llm_prompts: List[str] = inputs["prompts"]
            llm_output = llm.generate(llm_prompts, callbacks=[langchain_tracer])
        elif isinstance(llm, BaseChatModel):
            chat_prompts: List[str] = inputs["prompts"]
            messages = [
                parse_chat_messages(chat_prompt) for chat_prompt in chat_prompts
            ]
            llm_output = llm.generate(messages, callbacks=[langchain_tracer])
        else:
            raise ValueError(f"Unsupported LLM type {type(llm)}")
        return llm_output

    @staticmethod
    def run_llm_or_chain(
        example: Example,
        langchain_tracer: LangChainTracerV2,
        llm_or_chain: Union[Chain, BaseLanguageModel],
        n_repetitions: int,
    ) -> Union[List[dict], List[str], List[LLMResult], List[ChatResult]]:
        """Run the chain synchronously."""
        previous_example_id = langchain_tracer.example_id
        langchain_tracer.example_id = example.id
        outputs = []
        for _ in range(n_repetitions):
            try:
                if isinstance(llm_or_chain, BaseLanguageModel):
                    output: Any = LangChainPlusClient.run_llm(
                        llm_or_chain, example.inputs, langchain_tracer
                    )
                else:
                    output = llm_or_chain.run(
                        example.inputs, callbacks=[langchain_tracer]
                    )
                outputs.append(output)
            except Exception as e:
                logger.warning(f"Chain failed for example {example.id}. Error: {e}")
                outputs.append({"Error": str(e)})
            finally:
                langchain_tracer.example_id = previous_example_id
        return outputs

    def run_on_dataset(
        self,
        dataset_name: str,
        llm_or_chain: Union[Chain, BaseLanguageModel],
        num_repetitions: int = 1,
        session_name: Optional[str] = None,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """Run the chain on a dataset and store traces to the specified session name.

        Args:
            dataset_name: Name of the dataset to run the chain on.
            llm_or_chain: Chain or language model to run over the dataset.
            num_repetitions: Number of times to run the chain on each example.
            session_name: Name of the session to store the traces in.
            verbose: Whether to print progress.

        Returns:
            A dictionary mapping example ids to the chain outputs.
        """
        if isinstance(llm_or_chain, BaseLanguageModel):
            raise NotImplementedError
        if session_name is None:
            session_name = (
                f"{dataset_name}-{llm_or_chain.__class__.__name__}-{num_repetitions}"
            )
        dataset = self.read_dataset(dataset_name=dataset_name)
        examples = self.list_examples(dataset_id=str(dataset.id))
        results: Dict[str, Any] = {}
        with tracing_v2_enabled(session_name=session_name) as session:
            tracer = LangChainTracerV2()
            tracer.session = session

            for i, example in enumerate(examples):
                result = self.run_llm_or_chain(
                    example,
                    tracer,
                    llm_or_chain,
                    num_repetitions,
                )
                if verbose:
                    print(f"{i+1} processed", flush=True)
            results[example.id] = result
        return results
