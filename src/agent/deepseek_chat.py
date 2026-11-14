"""Preserve DeepSeek's reasoning field across Chat Completions tool calls."""
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI


class DeepSeekChat(ChatOpenAI):
    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, data["choices"]):
            reasoning = choice["message"].get("reasoning_content")
            if isinstance(reasoning, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _get_request_payload(self, input_, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, **kwargs)
        for message, wire in zip(messages, payload["messages"]):
            if isinstance(message, AIMessage):
                wire["reasoning_content"] = message.additional_kwargs.get("reasoning_content", "")
        return payload
