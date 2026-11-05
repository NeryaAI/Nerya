"""Public terminal copy, separate from preserved execution diagnostics.

Only lifecycle categories and observed operation counts enter this renderer.
Tool payloads, provider bodies and model fragments are never final conclusions.
This module does not decide completion, retries, permissions or recovery.
"""
from __future__ import annotations


def recorded_tool_count(transcript: list[dict]) -> int:
    return sum(1 for message in transcript if isinstance(message, dict)
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(part, dict) and part.get("type") == "tool_result")


def render_outcome_summary(*, reason: str = "aborted", user_text: str = "",
                           tool_calls: int = 0, error_count: int = 0,
                           had_model_text: bool = False, language: str | None = None,
                           status_code: int | None = None) -> str:
    zh = language == "zh" or (language is None and any("㐀" <= c <= "鿿" for c in user_text))
    exact = reason
    reason = reason.split(":", 1)[0]
    if status_code == 429:
        reason = "rate_limited"
    elif status_code in {401, 403}:
        reason = "model_auth_failed"
    groups = {
        "cancelled:operator_cancel": ("任务已停止，尚未完成。", "The task was stopped before completion."),
        "cancelled:command_control_unavailable": ("任务控制暂时不可用，系统已停止执行，并非用户主动取消。", "Task controls became unavailable, so the system stopped execution. This was not a user cancellation."),
        "cancelled:command_lease_lost": ("执行租约已丢失，任务已停止，结果尚未确认。", "The execution lease was lost. The task stopped and its result is unconfirmed."),
        "cancelled:event_persistence_failed": ("执行记录未能可靠保存，系统已停止任务。", "The system stopped because execution records could not be saved reliably."),
        "cancelled": ("任务已停止，尚未完成。", "The task stopped before completion."),
        "stream_interrupted": ("模型响应中断，本轮未能完成。", "The model response was interrupted before this task could finish."),
        "model_stream_interrupted": ("模型响应中断，本轮未能完成。", "The model response was interrupted before this task could finish."),
        "model_reasoning_repetition": ("模型推理陷入重复，本轮已停止，任务尚未完成。", "The model reasoning became repetitive. This run stopped before completing the task."),
        "timeout": ("等待响应超时，本轮未能完成。", "The task could not finish because a response timed out."),
        "request_timeout": ("等待响应超时，本轮未能完成。", "The task could not finish because a response timed out."),
        "rate_limited": ("模型服务暂时限流，本轮未能完成。", "The model service is rate-limited, so this task could not finish."),
        "model_quota_exhausted": ("模型服务额度不足，本轮未能完成。", "The model service has insufficient quota to finish this task."),
        "model_auth_failed": ("模型连接未通过验证，本轮未能完成。", "The model connection could not be authenticated, so this task could not finish."),
        "model_output_truncated": ("模型输出达到长度限制，本轮未能完成。", "The model reached its output limit before the task finished."),
        "interrupted_max_tokens": ("模型输出达到长度限制，本轮未能完成。", "The model reached its output limit before the task finished."),
        "interrupted_length": ("模型输出达到长度限制，本轮未能完成。", "The model reached its output limit before the task finished."),
        "provider_content_filtered": ("模型服务未返回可用结果，本轮未能完成。", "The model service did not return a usable result, so this task could not finish."),
        "empty_model_response": ("模型未返回有效回复，本轮未能完成。", "The model did not return a usable response, so this task could not finish."),
        "max_iterations": ("本轮已达到执行上限，任务尚未完成。", "This run reached its execution limit before completing the task."),
        "token_budget_exceeded": ("本轮已达到执行预算，任务尚未完成。", "This run reached its budget before completing the task."),
        "repeated_tool_call": ("同一步骤重复执行仍无进展，已停止继续尝试，任务尚未完成。", "Repeating the same step made no progress, so further attempts stopped. The task is incomplete."),
        "task_poll_no_progress": ("等待期间没有新的进展，本轮已停止。", "No further progress was observed while waiting, so this run stopped."),
        "diminishing_returns": ("执行没有取得新的有效进展，本轮已停止。", "Execution stopped because it was no longer making useful progress."),
        "no_final_response": ("本轮未形成完整结论，任务完成情况尚未确认。", "This run did not produce a complete conclusion. Task completion is not confirmed."),
    }
    generic = ("执行遇到问题，本轮未能完成。", "Execution encountered a problem and could not finish this task.")
    title = groups.get(exact, groups.get(reason, generic))[0 if zh else 1]
    paragraphs = [title]
    if tool_calls:
        paragraphs.append("本轮已有操作记录，但这些结果不足以确认整个任务完成。" if zh else
                          "Operations were recorded during this run, but they do not establish that the whole task is complete.")
    elif had_model_text:
        paragraphs.append("已有输出不完整，不能作为最终结论。" if zh else
                          "The available output is incomplete and is not a final conclusion.")
    if reason == "model_reasoning_repetition":
        next_step = ("请先调整模型设置或选择其他模型；继续前查看已有进度，避免重复执行已生效的操作。" if zh else
                     "Adjust the model settings or select another model. Review the existing progress before continuing to avoid repeating operations that already took effect.")
    elif reason in {"rate_limited", "model_quota_exhausted", "model_auth_failed"}:
        next_step = ("请先恢复模型服务，再从已保存的进度继续。" if zh else
                     "Restore model access before continuing from the saved progress.")
    elif reason == "cancelled":
        next_step = ("需要继续时，先查看已保存的进度；可能已生效的操作需先核对。" if zh else
                     "Before continuing, review the saved progress and verify any operations that may already have taken effect.")
    elif reason in {"repeated_tool_call", "diminishing_returns", "task_poll_no_progress"} or error_count:
        next_step = ("下一步先处理受阻步骤，再从已有进度继续。" if zh else
                     "Resolve the blocked step before continuing from the existing progress.")
    else:
        next_step = ("继续前请先查看已有进度，避免重复执行可能已生效的操作。" if zh else
                     "Review the existing progress before continuing, to avoid repeating operations that may already have taken effect.")
    paragraphs.append(next_step + (" 具体原因和工具记录可在执行详情中查看。" if zh else
                                  " The cause and tool records are available in execution details."))
    return "\n\n".join(paragraphs)
