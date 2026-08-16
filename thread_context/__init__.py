from .store import ConversationStore
from .training_log import ConversationTraceStore

conversation_store = ConversationStore()
conversation_trace_store = ConversationTraceStore()

__all__ = ["conversation_store", "conversation_trace_store"]
