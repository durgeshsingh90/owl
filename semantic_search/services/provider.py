"""Local FastEmbed provider with deterministic vector serialization."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from django.conf import settings


class EmbeddingProviderError(RuntimeError):
    """Raised when the configured local embedding model cannot produce safe vectors."""


class EmbeddingProvider(Protocol):
    """Small provider boundary used by workers and semantic query execution."""

    @property
    def version(self) -> str: ...

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray: ...

    def encode_query(self, text: str) -> np.ndarray: ...


@dataclass(frozen=True, slots=True)
class SerializedVector:
    value: bytes
    dimensions: int


class FastEmbedEmbeddingProvider:
    """Generate retrieval embeddings locally without uploading source text."""

    def __init__(self) -> None:
        self._model = None
        self._model_lock = threading.Lock()

    @property
    def version(self) -> str:
        return str(settings.SEMANTIC_MODEL_VERSION)

    def _load_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                from fastembed import TextEmbedding
                from huggingface_hub import snapshot_download

                configured_path = str(settings.SEMANTIC_MODEL_PATH or "").strip()
                if configured_path:
                    model_path = Path(configured_path).expanduser().resolve()
                    if not model_path.is_dir():
                        raise EmbeddingProviderError(
                            "The configured local semantic model folder is unavailable."
                        )
                else:
                    model_path = Path(
                        snapshot_download(
                            repo_id=str(settings.SEMANTIC_MODEL_REPOSITORY),
                            revision=str(settings.SEMANTIC_MODEL_REVISION),
                            cache_dir=str(settings.SEMANTIC_MODEL_CACHE_ROOT),
                            local_files_only=bool(settings.SEMANTIC_MODEL_OFFLINE),
                        )
                    )
                self._model = TextEmbedding(
                    model_name=str(settings.SEMANTIC_MODEL_ID),
                    cache_dir=str(settings.SEMANTIC_MODEL_CACHE_ROOT),
                    threads=1,
                    providers=["CPUExecutionProvider"],
                    specific_model_path=str(model_path),
                    local_files_only=True,
                )
            except EmbeddingProviderError:
                raise
            except Exception as exc:
                raise EmbeddingProviderError(
                    "OWL could not load the configured local semantic model."
                ) from exc
        return self._model

    def encode_documents(self, texts: Sequence[str]) -> np.ndarray:
        normalized = tuple(str(text or "").strip() for text in texts)
        if not normalized or any(not text for text in normalized):
            raise EmbeddingProviderError("Embedding input must contain non-empty text.")
        try:
            vectors = tuple(
                self._load_model().passage_embed(
                    normalized,
                    batch_size=int(settings.SEMANTIC_EMBEDDING_BATCH_SIZE),
                    parallel=None,
                )
            )
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            raise EmbeddingProviderError("The local semantic model could not encode text.") from exc
        array = np.asarray(vectors, dtype=np.float32)
        if array.ndim != 2 or array.shape[0] != len(normalized) or array.shape[1] < 1:
            raise EmbeddingProviderError("The local semantic model returned invalid dimensions.")
        if not np.isfinite(array).all():
            raise EmbeddingProviderError("The local semantic model returned invalid values.")
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        if np.any(norms <= 0):
            raise EmbeddingProviderError("The local semantic model returned an empty vector.")
        return np.ascontiguousarray(array / norms, dtype=np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        normalized = str(text or "").strip()
        if not normalized:
            raise EmbeddingProviderError("A semantic query cannot be empty.")
        try:
            vector = next(iter(self._load_model().query_embed(normalized)))
        except Exception as exc:
            raise EmbeddingProviderError(
                "The local semantic model could not encode a query."
            ) from exc
        array = np.asarray(vector, dtype=np.float32)
        if array.ndim != 1 or array.size < 1 or not np.isfinite(array).all():
            raise EmbeddingProviderError(
                "The local semantic model returned an invalid query vector."
            )
        norm = float(np.linalg.norm(array))
        if norm <= 0:
            raise EmbeddingProviderError("The local semantic model returned an empty query vector.")
        return np.ascontiguousarray(array / norm, dtype=np.float32)


_provider: FastEmbedEmbeddingProvider | None = None
_provider_lock = threading.Lock()


def get_embedding_provider() -> FastEmbedEmbeddingProvider:
    """Return one lazily loaded model instance per worker/web process."""

    global _provider
    if _provider is not None:
        return _provider
    with _provider_lock:
        if _provider is None:
            _provider = FastEmbedEmbeddingProvider()
    return _provider


def serialize_vector(vector: np.ndarray) -> SerializedVector:
    """Serialize one normalized vector as portable little-endian float32 bytes."""

    array = np.asarray(vector, dtype=np.float32)
    if array.ndim != 1 or array.size < 1 or not np.isfinite(array).all():
        raise EmbeddingProviderError("A semantic vector could not be serialized safely.")
    little_endian = np.ascontiguousarray(array.astype("<f4", copy=False))
    return SerializedVector(value=little_endian.tobytes(), dimensions=int(little_endian.size))


def deserialize_vector(value: bytes | memoryview, *, dimensions: int) -> np.ndarray:
    """Decode and validate one persisted little-endian float32 vector."""

    if dimensions < 1:
        raise EmbeddingProviderError("A persisted semantic vector has invalid dimensions.")
    raw = bytes(value)
    if len(raw) != dimensions * 4:
        raise EmbeddingProviderError("A persisted semantic vector has an invalid byte length.")
    vector = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=False)
    if not np.isfinite(vector).all():
        raise EmbeddingProviderError("A persisted semantic vector contains invalid values.")
    return vector
