from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any, TypeVar

from pydantic import BaseModel
from strands import Agent
from strands.models.model import Model
from strands.types.content import ContentBlock, Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from rllm.agents.agent import Step, Trajectory
from rllm.engine import ModelOutput, RolloutEngine

T = TypeVar("T", bound=BaseModel)


class RLLMModel(Model):
    """Model class that uses rLLM's RolloutEngine for inference."""

    def __init__(self, rollout_engine: RolloutEngine, **kwargs):
        """Initialize the RLLMModel.

        Args:
            rollout_engine: The rLLM RolloutEngine instance to use for inference
        """
        self.rollout_engine = rollout_engine
        self.kwargs = kwargs

    async def structured_output(self, output_model: type[T], prompt: Messages, system_prompt: str | None = None, **kwargs: Any) -> AsyncGenerator[dict[str, T | Any], None]:
        """Get structured output from the model.

        Note: This is a basic implementation that converts the text response to the output model.
        For more advanced structured output, consider using the native OpenAI structured output features.

        Args:
            output_model: The output model to use for the agent.
            prompt: The prompt messages to use for the agent.
            system_prompt: System prompt to provide context to the model.
            **kwargs: Additional keyword arguments.

        Yields:
            Model events with the last being the structured output.
        """
        # Convert Strands messages to chat completion format
        messages = self._convert_messages_to_chat_format(prompt, system_prompt)

        # Add instruction for structured output
        if messages and messages[-1]["role"] == "user":
            original_content = messages[-1]["content"]
            messages[-1]["content"] = f"{original_content}\n\nPlease respond with a JSON object that matches this schema: {output_model.model_json_schema()}"

        # Get response from rollout engine
        response_text = (await self.rollout_engine.get_model_response(messages, **kwargs)).text

        try:
            # Try to parse the response as JSON and convert to the output model
            import json

            # Extract JSON from response if it's wrapped in other text
            json_start = response_text.find("{")
            json_end = response_text.rfind("}") + 1

            if json_start >= 0 and json_end > json_start:
                json_str = response_text[json_start:json_end]
                parsed_data = json.loads(json_str)
                structured_output = output_model(**parsed_data)
                yield {"output": structured_output}
            else:
                raise ValueError("No valid JSON found in response")

        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(f"Failed to parse structured output: {e}") from e

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        """Stream conversation with the model using RolloutEngine.

        Args:
            messages: List of message objects to be processed by the model.
            tool_specs: List of tool specifications to make available to the model.
            system_prompt: System prompt to provide context to the model.
            **kwargs: Additional keyword arguments.

        Yields:
            Formatted message chunks from the model.
        """
        # Convert Strands messages to chat completion format
        chat_messages = self._convert_messages_to_chat_format(messages, system_prompt)

        # TODO: Handle tool_specs - for now we'll log a warning if they're provided
        if tool_specs:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning("Tool specs are not yet supported with RLLMModel")

        # Yield message start
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}

        # Get response from rollout engine
        model_output: ModelOutput = await self.rollout_engine.get_model_response(chat_messages, **kwargs)

        # Extract text from ModelOutput
        response_text = model_output.text

        # Simulate streaming by yielding the response in chunks
        # In a real streaming implementation, you'd want to modify RolloutEngine to support streaming
        chunk_size = 50  # Adjust as needed
        for i in range(0, len(response_text), chunk_size):
            chunk = response_text[i : i + chunk_size]
            yield {"contentBlockDelta": {"delta": {"text": chunk}}}

        # Yield message end
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": model_output.finish_reason or "end_turn"}}

        # Add usage metadata from ModelOutput
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": model_output.prompt_tokens,
                    "outputTokens": model_output.completion_tokens,
                    "totalTokens": model_output.prompt_tokens + model_output.completion_tokens,
                },
                "metrics": {
                    "latencyMs": 0,
                },
            }
        }

    def _convert_messages_to_chat_format(self, messages: Messages, system_prompt: str | None = None) -> list[dict[str, str]]:
        """Convert Strands messages to chat completion format.

        This reuses logic similar to OpenAIModel but outputs the simpler format expected by RolloutEngine.

        Args:
            messages: Strands messages to convert
            system_prompt: Optional system prompt to prepend

        Returns:
            List of chat completion messages
        """
        chat_messages = []

        # Add system prompt if provided
        if system_prompt:
            chat_messages.append({"role": "system", "content": system_prompt})

        for message in messages:
            role = message["role"]
            content = message["content"]

            # Extract text content from Strands format
            text_content = ""
            for content_block in content:
                if "text" in content_block:
                    text_content += content_block["text"]
                elif "toolUse" in content_block:
                    # For now, represent tool use as text
                    tool_use = content_block["toolUse"]
                    text_content += f"[Tool: {tool_use['name']} with input: {tool_use.get('input', {})}]"
                elif "toolResult" in content_block:
                    # For now, represent tool result as text
                    tool_result = content_block["toolResult"]
                    text_content += f"[Tool Result: {tool_result.get('content', [])}]"
                # TODO: Handle other content types like images, documents if needed

            if text_content.strip():  # Only add if there's actual content
                chat_messages.append({"role": role, "content": text_content})

        return chat_messages


class StrandsAgent(Agent):
    def __init__(self, model: RLLMModel, **kwargs):
        """Initialize StrandsAgent with trajectory tracking.

        Args:
            **kwargs: Additional arguments to pass to the base Agent class
        """

        super().__init__(model=model, **kwargs)
        self._trajectory = Trajectory()
        self._current_step = None

    @property
    def trajectory(self) -> Trajectory:
        """Get the current trajectory object."""
        return self._trajectory

    @property
    def chat_completions(self):
        """Convert agent's messages into chat completions format."""
        completions = []
        for message in self.messages:
            # Convert Strands message format to chat completion format
            if isinstance(message.get("content"), list):
                # Handle multi-content messages
                text_content = ""
                for content_block in message["content"]:
                    if isinstance(content_block, dict) and "text" in content_block:
                        text_content += content_block["text"]
                completions.append({"role": message["role"], "content": text_content})
            else:
                # Handle simple string content
                completions.append({"role": message["role"], "content": str(message.get("content", ""))})
        return completions

    def reset_trajectory(self, task: Any = None):
        """Reset the trajectory for a new episode."""
        self._trajectory = Trajectory(task=task)
        self._current_step = None

    def _start_new_step(self, observation: Any = None):
        """Start a new step in the trajectory."""
        self._current_step = Step(chat_completions=self.chat_completions.copy(), observation=observation)

    def _finish_current_step(self, model_response: str = "", action: Any = None, reward: float = 0.0, done: bool = False):
        """Finish the current step and add it to the trajectory."""
        if self._current_step is not None:
            self._current_step.model_response = model_response
            self._current_step.action = action
            self._current_step.reward = reward
            self._current_step.done = done
            self._current_step.chat_completions = self.chat_completions.copy()

            self._trajectory.steps.append(self._current_step)
            self._trajectory.reward += reward
            self._current_step = None

    def __call__(self, prompt: str | list[ContentBlock], **kwargs) -> Any:
        """Enhanced call method that tracks trajectory."""
        # Start a new step with the user prompt as observation
        self._start_new_step(observation=prompt)

        # Call the original Strands Agent logic
        result = super().__call__(prompt, **kwargs)

        # Extract relevant information from the result
        model_response = ""
        action = None

        if hasattr(result, "message") and result.message:
            # Extract text content from the final message
            if hasattr(result.message, "content") and isinstance(result.message.content, list):
                for content_block in result.message.content:
                    if isinstance(content_block, dict) and "text" in content_block:
                        model_response += content_block["text"]
            elif hasattr(result.message, "content"):
                model_response = str(result.message.content)

            # The action could be the final message or result itself
            action = result.message if hasattr(result, "message") else result

        # Determine if this step is done (could be based on stop_reason or other criteria)
        done = hasattr(result, "stop_reason") and result.stop_reason in ["end_turn", "stop_sequence"]

        # Finish the current step
        self._finish_current_step(model_response=model_response, action=action, done=done)

        return result

    async def invoke_async(self, prompt: str | list[ContentBlock], **kwargs) -> Any:
        """Enhanced async invoke method that tracks trajectory."""
        # Start a new step with the user prompt as observation
        self._start_new_step(observation=prompt)

        # Call the original Strands Agent async logic
        result = await super().invoke_async(prompt, **kwargs)

        # Extract relevant information from the result (same logic as __call__)
        model_response = ""
        action = None

        if hasattr(result, "message") and result.message:
            if hasattr(result.message, "content") and isinstance(result.message.content, list):
                for content_block in result.message.content:
                    if isinstance(content_block, dict) and "text" in content_block:
                        model_response += content_block["text"]
            elif hasattr(result.message, "content"):
                model_response = str(result.message.content)

            action = result.message if hasattr(result, "message") else result

        done = hasattr(result, "stop_reason") and result.stop_reason in ["end_turn", "stop_sequence"]

        # Finish the current step
        self._finish_current_step(model_response=model_response, action=action, done=done)

        return result

    def get_current_state(self) -> Step | None:
        """Get the current step state."""
        if self._trajectory.steps:
            return self._trajectory.steps[-1]
        return self._current_step

    def update_step_reward(self, reward: float):
        """Update the reward for the current or last step."""
        if self._current_step is not None:
            self._current_step.reward = reward
        elif self._trajectory.steps:
            self._trajectory.steps[-1].reward = reward
            # Update trajectory total reward
            self._trajectory.reward = sum(step.reward for step in self._trajectory.steps)

    def update_step_info(self, info: dict):
        """Update the info for the current or last step."""
        if self._current_step is not None:
            self._current_step.info.update(info)
        elif self._trajectory.steps:
            self._trajectory.steps[-1].info.update(info)
