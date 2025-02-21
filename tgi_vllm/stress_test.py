import requests
import json
import time
import random
from typing import List
import multiprocessing
import sseclient

def get_vllm_response(query, context=None):
    url = "http://localhost:8000/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        # "Authorization": "EMPTY"
    }
    data = {
        "model": "Qwen",
        "messages": [
            {"role": "system", "content": "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."},
             {"role": "user", "content": query}
        ],
        "stream": True, # 这里设置成流式输出
        "max_tokens": 16, #最大生产的token数量
    }
    time_st = int(time.time() * 1000) # 请求开始时间
    response = requests.post(url, headers=headers, json=data, stream=True)
    # print("response1=", response)
    # print("response2=", response.json())
    # print("response.headers.get('content-type')=", response.headers.get('content-type'))

    event_data = {} # 保存事件
    first_token_cost = None # 保存首字符时间
    if response.status_code == 200:
        if response.headers.get('content-type') == 'text/event-stream; charset=utf-8':  # 判断是否为流式响应
            client = sseclient.SSEClient(response.iter_content())  # 创建sse客户端 实例以处理事件流
            # print("client.events()=",client.events())  <generator object SSEClient.events at 0x7f44bd565f50>
            for event in client.events():  # 循环解析事件
                # print("event.data=", event.data, type(event.data))
                if("DONE" not in event.data):
                    event_data = json.loads(event.data)  # 解析事件数据
                    if first_token_cost is None:  # 如果还没有记录首字符时间
                        first_token_cost = int(time.time() * 1000) - time_st  # 计算首包延迟，TTFT
                else:
                    break  # 如果接受到结束标志和生成的文本，则退出循环
        else:
            event_data = response.json()  # 不是stream返回，直接解析json数据
    event_data['query'] = query  # 存储query
    event_data['first_token_cost'] = first_token_cost  # 记录首字符的消耗
    if event_data.get('token'):
        event_data.pop('token')  # 如果存在token数据，则移除
    return event_data


def get_tgi_response(query, context=None):
    """
    与TGI服务器交互获取响应。
    发送查询到TGI服务器，并根据服务器的响应生成合适的回答。
    记录首次生成token的时间，用于计算延迟。
    参数:
    - query: 用户的查询字符串。
    - context: 上下文信息（此函数中未使用）。
    返回:
    - 包含服务器响应数据的字典，包括查询、首次token生成时间和去除token数据。
    """
    url = "http://127.0.0.1:9001/generate_stream" # 模型服务器的URL，这里端口要对应好，9001是默认端口，根据实际情况修改。
    headers = {"Content-Type": "application/json"}
    data = {
        "inputs": query, # 查询内容
        "parameters": {
            "max_new_tokens": 16 , #最大生产的token数量
            "do_sample": False, # 是否使用采样
        }
    }
    time_st = int(time.time() * 1000) # 请求开始时间
    response = requests.post(url, headers=headers, data=json.dumps(data),
                             stream=True) # 发送post请求
    event_data = {} # 保存事件
    first_token_cost = None # 保存首字符时间
    if response.status_code == 200:
        if response.headers.get('content-type') == 'text/event-stream': # 判断是否为流式响应
            client = sseclient.SSEClient(response.iter_content()) # 创建sse客户端 实例以处理事件流
            for event in client.events(): # 循环解析事件
                event_data = json.loads(event.data) # 解析事件数据
                if first_token_cost is None: # 如果还没有记录首字符时间
                    first_token_cost = int(time.time() * 1000) - time_st # 计算首包延迟，TTFT
                if event_data.get('end') or event_data.get('generated_text'):
                    break # 如果接受到结束标志和生成的文本，则退出循环
        else:
            event_data = response.json() # 不是stream返回，直接解析json数据
    event_data['query'] = query #存储query
    event_data['first_token_cost'] = first_token_cost # 记录首字符的消耗
    if event_data.get('token'):
        event_data.pop('token') # 如果存在token数据，则移除
    return event_data

def calculate_percentile(data, percentile):
    """
    计算给定数据的百分位数。
    参数:
    - data: 数据列表。
    - percentile: 百分位数。
    返回:
    - 在给定百分位数上的数据值。
    """
    data = sorted(data) # 排序
    index = int(len(data) * percentile) # 计算百分数的索引
    return data[index]

def worker_function(query, start_time):
    """
    工作线程函数，发送查询并处理响应。
    参数:
    - query: 用户的查询字符串。
    - start_time: 所有请求预期的开始时间。
    返回:
    - 包含响应数据的字典，包括开始和结束时间戳及请求耗时。
    """
    time.sleep(max((start_time - time.time()), 0.001)) # 等待直到指定的时间
    time_st = time.time() # 请求开始时间（秒）
    # response_data = get_tgi_response(query) # 核心函数，获取模型服务响应
    response_data = get_vllm_response(query) # 核心函数，获取模型服务响应
    response_data["st"] = int(time_st * 1000) # 请求开始时间（毫秒）
    response_data["ed"] = int(time.time() * 1000) # 请求结束时间（毫秒）
    response_data["request_cost"] = response_data["ed"] - response_data["st"] # 计算请求消耗时间 （毫秒）
    # print(json.dumps(response_data, ensure_ascii=False)) # 输出响应的数据
    return response_data

if __name__ == "__main__":
    query_list = ["呼吸道能够使气体变得清洁的原因"] # 查询内容列表
    num_processes = 100 # 并行进程数
    max_qps = 300 # 每秒最大请求数
    total_requests = 500 #总请求数

    with multiprocessing.Pool(num_processes) as pool:
        querys = random.choices(query_list, k=total_requests) # 随机选择查询
        worker_start = time.time() # 开始的时间
        tasks = [(x, worker_start + ix/max_qps) for ix, x in enumerate(querys)] #任务列表，每个任务包含查询和计划开始时间
        results = pool.starmap(worker_function, tasks) # 启动工作进程处理任务

    start_time = min([d["st"] for d in results]) # 获取所有请求的开始时间的最小值
    end_time = max([d["ed"] for d in results]) # 获取所有请求结束时间的最大值
    total_time = end_time - start_time # 总消耗时间（毫秒）
    qps = len(results) / total_time * 1000 #计算每秒请求数QPS
    print(f"QPS: {qps}")

    example = results[0]
    if example.get('request_cost'):
        request_costs = [data['request_cost'] for data in results] # 所有请求成本
        avg_request_cost_service = sum([data['request_cost'] for data in results])/ len(results) # 平均请求成本
        p95_request_cost = calculate_percentile(request_costs, 0.95) # 95百分位请求成本
        p99_request_cost = calculate_percentile(request_costs, 0.99) # 99百分位请求成本
        print(f"Avg request cost service: {avg_request_cost_service:.0f}ms")
        print(f"P95 request cost: {p95_request_cost:.0f}ms")
        print(f"P99 request cost: {p99_request_cost:.0f}ms")

    if example.get('first_token_cost'):
        first_token_costs = [data['first_token_cost'] for data in results] # 所有请求首包延迟
        avg_first_token_cost_service = sum([data['first_token_cost'] for data in results]) / len(results) # 平均首包延迟
        p95_first_token_cost = calculate_percentile(first_token_costs, 0.95) # 95百分位首包延迟
        p99_first_token_cost = calculate_percentile(first_token_costs, 0.99) # 99百分位首包延迟
        print(f"P95 time first token cost: {p95_first_token_cost:.0f}ms")
        print(f"P99 time first token cost: {p99_first_token_cost:.0f}ms")
        print(f"Avg first token cost service:{avg_first_token_cost_service:.0f}ms")