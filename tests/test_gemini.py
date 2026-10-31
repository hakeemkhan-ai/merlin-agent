import unittest
from copy import deepcopy
from types import SimpleNamespace

from google.genai import types
from pydantic import SecretStr

from merlin.config import Settings
from merlin.contracts import BudgetExceeded, Task, TaskSpec
from merlin.integrations.gemini import GeminiClient


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    async def test_counting_and_generation_share_deduplicated_history_without_losing_metadata(self):
        text = "A long file's content " * 500
        history = [
            {
                "role": "model",
                "parts": [
                    {
                        "function_call": {"id": "call-1", "name": "read_file", "args": {}},
                        "thought_signature": b"signed-state",
                    }
                ],
                "_merlin_calls": [{"id": "call-1"}],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "id": "call-1",
                            "name": "read_file",
                            "response": {
                                "output": text,
                                "ok": True,
                                "uncertain": False,
                                "data": {"output": text, "sha256": "checksum", "total_lines": 500},
                            },
                        }
                    },
                    {
                        "function_response": {
                            "id": "call-2",
                            "name": "read_file",
                            "response": {"output": "preview", "data": {"output": "different"}},
                        }
                    },
                ],
            },
        ]
        original = deepcopy(history)
        captured = {}

        async def count_tokens(**kwargs):
            captured["counted"] = kwargs["contents"]
            return types.CountTokensResponse(total_tokens=100)

        async def generate(**kwargs):
            captured["generated"] = kwargs["contents"]

            async def chunks():
                yield types.GenerateContentResponse(
                    candidates=[
                        types.Candidate(
                            content=types.Content(role="model", parts=[types.Part(text="Done")]),
                            finish_reason="STOP",
                        )
                    ]
                )

            return chunks()

        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(generate_content_stream=generate, count_tokens=count_tokens)
            )
        )
        adapter = GeminiClient(Settings(), client=client)
        await adapter.generate(
            Task(spec=TaskSpec(prompt="read", repository="owner/repo")), history, []
        )
        sent = captured["generated"]
        self.assertEqual(captured["counted"][1:], sent)
        self.assertEqual(history, original)
        self.assertEqual(sent[0].parts[0].thought_signature, b"signed-state")
        self.assertEqual(sent[0].parts[0].function_call.id, "call-1")
        result = sent[1].parts[0].function_response.response
        self.assertEqual(result["output"], text)
        self.assertEqual(result["data"], {"sha256": "checksum", "total_lines": 500})
        self.assertTrue(result["ok"])
        self.assertFalse(result["uncertain"])
        self.assertEqual(sent[1].parts[1].function_response.response["data"]["output"], "different")

    async def test_native_signatures_and_function_ids_survive(self):
        captured = {}

        async def generate(**kwargs):
            captured.update(kwargs)

            async def chunks():
                yield types.GenerateContentResponse(
                    candidates=[
                        types.Candidate(
                            finish_reason="STOP",
                            content=types.Content(
                                role="model",
                                parts=[
                                    types.Part(
                                        function_call=types.FunctionCall(
                                            name="read_file", args={"path": "main.py"}
                                        ),
                                        thought_signature=b"opaque-state",
                                    )
                                ],
                            ),
                        )
                    ],
                    usage_metadata=types.GenerateContentResponseUsageMetadata(
                        prompt_token_count=10, candidates_token_count=5, thoughts_token_count=3
                    ),
                )

            return chunks()

        client = SimpleNamespace(
            aio=SimpleNamespace(
                models=SimpleNamespace(
                    generate_content_stream=generate, count_tokens=self.count_tokens
                )
            )
        )
        adapter = GeminiClient(Settings(gemini_api_key=SecretStr("test")), client=client)
        task = Task(spec=TaskSpec(prompt="read", repository="owner/repo"))
        reply = await adapter.generate(task, [{"role": "user", "parts": [{"text": "read"}]}], [])
        self.assertEqual(reply.output_tokens, 8)
        restored = types.Content.model_validate(reply.content)
        self.assertEqual(restored.parts[0].thought_signature, b"opaque-state")
        self.assertEqual(restored.parts[0].function_call.id, reply.calls[0].id)
        self.assertTrue(captured["config"].automatic_function_calling.disable)

    @staticmethod
    async def count_tokens(**kwargs):
        return types.CountTokensResponse(total_tokens=10)

    async def test_oversized_context_is_rejected_before_generation(self):
        async def count_tokens(**kwargs):
            return types.CountTokensResponse(total_tokens=65536)

        client = SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(count_tokens=count_tokens))
        )
        adapter = GeminiClient(Settings(), client=client)
        task = Task(spec=TaskSpec(prompt="read", repository="owner/repo"))
        with self.assertRaises(BudgetExceeded):
            await adapter.generate(task, [{"role": "user", "parts": [{"text": "read"}]}], [])
