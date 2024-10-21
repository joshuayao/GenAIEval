# Copyright (C) 2024 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
import sys
import threading
import time

import gevent
import sseclient
from locust import HttpUser, between, events, task
from locust.runners import STATE_CLEANUP, STATE_STOPPED, STATE_STOPPING, MasterRunner, WorkerRunner

cwd = os.path.dirname(__file__)
sys.path.append(cwd)


@events.init_command_line_parser.add_listener
def _(parser):
    parser.add_argument(
        "--max-request",
        type=int,
        env_var="MAX_REQUEST",
        default=-1,
        help="Stop the benchmark If exceed this request",
    )
    parser.add_argument(
        "--http-timeout", type=int, env_var="HTTP_TIMEOUT", default=120000, help="Http timeout before receive response"
    )
    parser.add_argument(
        "--bench-target",
        type=str,
        env_var="BENCH_TARGET",
        default="chatqnafixed",
        help="python package name for benchmark target",
    )
    parser.add_argument(
        "--llm-model",
        type=str,
        env_var="LLM_MODEL",
        default="Intel/neural-chat-7b-v3-3",
        help="LLM model name",
    )
    parser.add_argument(
        "--load-shape",
        type=str,
        env_var="OPEA_EVAL_LOAD_SHAPE",
        default="constant",
        help="load shape to adjust conccurency at runtime",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        env_var="OPEA_EVAL_DATASET",
        default="default",
        help="dataset",
    )
    parser.add_argument(
        "--seed",
        type=str,
        env_var="OPEA_EVAL_SEED",
        default="none",
        help="The seed for all RNGs",
    )
    parser.add_argument(
        "--prompts",
        type=str,
        env_var="OPEA_EVAL_PROMPTS",
        default="In a increasingly complex world where technology has rapidly advanced and evolved far beyond our wildest dreams and most ambitious imaginations, humanity now stands on the very precipice of a revolutionary new era that is filled with endless possibilities, profound and significant changes, as well as intricate challenges that we must address. The year is now 2050, and artificial intelligence has seamlessly woven itself deeply and intricately into the very fabric of everyday life. Autonomous vehicles glide effortlessly and smoothly through the bustling, vibrant, and lively city streets, while drones swiftly and accurately deliver packages with pinpoint precision, making logistics and delivery systems more efficient, streamlined, and advanced than ever before in the entire history of humankind and technological development. Smart homes, equipped with cutting-edge advanced sensors and sophisticated algorithms, anticipate every possible need and requirement of their inhabitants, creating an environment of unparalleled convenience, exceptional comfort, and remarkable efficiency that enhances our daily lives. However, with these remarkable and groundbreaking advancements come a host of new challenges, uncertainties, and ethical dilemmas that society must confront, navigate, and address in a thoughtful and deliberate manner. As we carefully navigate through this brave new world filled with astonishing technological marvels, innovations, and breakthroughs, questions about the implications and consequences of AI technologies become increasingly pressing, relevant, and urgent for individuals and communities alike. Issues surrounding privacy—how our personal data is collected, securely stored, processed, and utilized—emerge alongside significant concerns about security in a rapidly evolving digital landscape where vulnerabilities can be easily and readily exploited by malicious actors, hackers, and cybercriminals. Moreover, philosophical inquiries regarding the very nature of consciousness itself rise prominently to the forefront of public discourse, debate, and discussion, inviting diverse perspectives, opinions, and ethical considerations from various stakeholders. In light of these profound developments and transformative changes that we are witnessing, I would like to gain a much deeper, broader, and more comprehensive understanding of what artificial intelligence truly is and what it encompasses in its entirety and complexity. Could you elaborate extensively, thoroughly, and comprehensively on its precise definition, its wide-ranging and expansive scope, as well as the myriad and diverse ways it significantly impacts our daily lives, personal experiences, and society as a whole in various dimensions?",
        help="User-customized prompts",
    )
    parser.add_argument(
        "--max-output",
        type=int,
        env_var="OPEA_EVAL_MAX_OUTPUT_TOKENS",
        default=128,
        help="Max number of output tokens"
    )

reqlist = []
start_ts = 0
end_ts = 0
req_total = 0
last_resp_ts = 0

bench_package = ""
console_logger = logging.getLogger("locust.stats_logger")


class AiStressUser(HttpUser):
    request = 0
    _lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @task
    def bench_main(self):
        max_request = self.environment.parsed_options.max_request
        if max_request >= 0 and AiStressUser.request >= max_request:
            # For custom load shape based on arrival_rate, new users spawned after exceeding max_request is reached will be stopped.
            # TODO: user should not care about load shape
            if "arrival_rate" in self.environment.parsed_options:
                self.stop(force=True)

            time.sleep(1)
            return
        with AiStressUser._lock:
            AiStressUser.request += 1
            self.environment.runner.send_message("worker_reqsent", 1)
        reqData = bench_package.getReqData()
        url = bench_package.getUrl()
        streaming_bench_target = [
            "llmfixed",
            "llmservefixed",
            "llmbench",
            "chatqnafixed",
            "chatqnabench",
            "codegenfixed",
            "codegenbench",
            "faqgenfixed",
            "faqgenbench",
        ]
        try:
            start_ts = time.perf_counter()
            with self.client.post(
                url,
                json=reqData,
                stream=True if self.environment.parsed_options.bench_target in streaming_bench_target else False,
                catch_response=True,
                timeout=self.environment.parsed_options.http_timeout,
            ) as resp:
                logging.debug("Got response...........................")

                if resp.status_code >= 200 and resp.status_code < 400:
                    if self.environment.parsed_options.bench_target in [
                        "embedservefixed",
                        "embeddingfixed",
                        "retrieverfixed",
                        "rerankservefixed",
                        "rerankingfixed",
                    ]:
                        respData = {
                            "total_latency": time.perf_counter() - start_ts,
                        }
                    elif self.environment.parsed_options.bench_target in [
                        "audioqnafixed",
                        "audioqnabench",
                    ]:  # non-stream case
                        respData = {
                            "response_string": resp.text,
                            "first_token_latency": time.perf_counter() - start_ts,
                            "total_latency": time.perf_counter() - start_ts,
                        }
                    else:
                        first_token_ts = None
                        complete_response = ""
                        if self.environment.parsed_options.bench_target == "llmservefixed":
                            client = sseclient.SSEClient(resp)
                            for event in client.events():
                                if first_token_ts is None:
                                    first_token_ts = time.perf_counter()
                                try:
                                    data = json.loads(event.data)
                                    if "choices" in data and len(data["choices"]) > 0:
                                        delta = data["choices"][0].get("delta", {})
                                        content = delta.get("content", "")
                                        complete_response += content
                                except json.JSONDecodeError:
                                    continue
                        else:
                            client = sseclient.SSEClient(resp)
                            for event in client.events():
                                if event.data == "[DONE]":
                                    break
                                else:
                                    if first_token_ts is None:
                                        first_token_ts = time.perf_counter()
                                    chunk = event.data.strip()
                                    if chunk.startswith("b'") and chunk.endswith("'"):
                                        chunk = chunk[2:-1]
                                complete_response += chunk
                        end_ts = time.perf_counter()
                        respData = {
                            "response_string": complete_response,
                            "first_token_latency": first_token_ts - start_ts,
                            "total_latency": end_ts - start_ts,
                        }
                    reqdata = bench_package.respStatics(self.environment, reqData, respData)
                    logging.debug(f"Request data collected {reqdata}")
                    self.environment.runner.send_message("worker_reqdata", reqdata)
            logging.debug("Finished response analysis...........................")
        except Exception as e:
            # In case of exception occurs, locust lost the statistic for this request.
            # Consider as a failed request, and report to Locust statistics
            logging.error(f"Failed with request : {e}")
            self.environment.runner.stats.log_request("POST", url, time.perf_counter() - start_ts, 0)
            self.environment.runner.stats.log_error("POST", url, "Locust Request error")

        # For custom load shape based on arrival_rate, a user only sends single request before it sleeps.
        # TODO: user should not care about load shape
        if "arrival_rate" in self.environment.parsed_options:
            time.sleep(365 * 60 * 60)

    # def on_stop(self) -> None:


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    if not isinstance(environment.runner, WorkerRunner):
        console_logger.info(f"Concurrency       : {environment.parsed_options.num_users}")
        console_logger.info(f"Max request count : {environment.parsed_options.max_request}")
        console_logger.info(f"Http timeout      : {environment.parsed_options.http_timeout}\n")
        console_logger.info(f"Benchmark target  : {environment.parsed_options.bench_target}\n")
        console_logger.info(f"Load shape        : {environment.parsed_options.load_shape}")
        console_logger.info(f"Dataset           : {environment.parsed_options.dataset}")
        console_logger.info(f"Customized prompt : {environment.parsed_options.prompts}")
        console_logger.info(f"Max output tokens : {environment.parsed_options.max_output}")


@events.init.add_listener
def on_locust_init(environment, **_kwargs):
    global bench_package
    os.environ["OPEA_EVAL_DATASET"] = environment.parsed_options.dataset
    os.environ["OPEA_EVAL_SEED"] = environment.parsed_options.seed
    os.environ["OPEA_EVAL_PROMPTS"] = environment.parsed_options.prompts
    os.environ["OPEA_EVAL_MAX_OUTPUT_TOKENS"] = str(environment.parsed_options.max_output)
    try:
        bench_package = __import__(environment.parsed_options.bench_target)
    except ImportError:
        return None
    if not isinstance(environment.runner, WorkerRunner):
        gevent.spawn(checker, environment)
        environment.runner.register_message("worker_reqdata", on_reqdata)
        environment.runner.register_message("worker_reqsent", on_reqsent)
    if not isinstance(environment.runner, MasterRunner):
        environment.runner.register_message("all_reqcnt", on_reqcount)
        environment.runner.register_message("test_quit", on_quit)


@events.quitting.add_listener
def on_locust_quitting(environment, **kwargs):
    if isinstance(environment.runner, WorkerRunner):
        logging.debug("Running in WorkerRunner, DO NOT print statistics")
        return

    logging.debug("#####Running in MasterRunner, DO print statistics")
    bench_package.staticsOutput(environment, reqlist)


def on_reqdata(msg, **kwargs):
    logging.debug(f"Request data: {msg.data}")
    reqlist.append(msg.data)


def on_reqsent(environment, msg, **kwargs):
    logging.debug(f"request sent: {msg.data}")
    global req_total
    req_total += 1
    environment.runner.send_message("all_reqcnt", req_total)


def on_reqcount(msg, **kwargs):
    logging.debug(f"Update total request: {msg.data}")
    AiStressUser.request = msg.data


def on_quit(environment, msg, **kwargs):
    logging.debug("Test quitting, set stop_timeout to 0...")
    environment.runner.environment.stop_timeout = 0


def checker(environment):
    while environment.runner.state not in [STATE_STOPPING, STATE_STOPPED, STATE_CLEANUP]:
        time.sleep(1)
        max_request = environment.parsed_options.max_request
        if max_request >= 0 and environment.runner.stats.num_requests >= max_request:
            logging.info(f"Exceed the max-request number:{environment.runner.stats.num_requests}, Exit...")
            # Remove stop_timeout after test quit to avoid Locust user stop exception with custom load shape
            environment.runner.send_message("test_quit", None)
            environment.runner.environment.stop_timeout = 0
            #            while environment.runner.user_count > 0:
            time.sleep(5)
            environment.runner.quit()
            return
