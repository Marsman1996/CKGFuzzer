import pandas as pd
import json
from llama_index.core.prompts import PromptTemplate
from loguru import logger
from pydantic import BaseModel
from typing import List
from llama_index.core.program import LLMTextCompletionProgram
from tqdm import tqdm
from llama_index.core.memory import (
    VectorMemory,
    SimpleComposableMemory,
    ChatMemoryBuffer,
)
from llama_index.core.llms import ChatMessage
from llama_index.core import Settings
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core import get_response_synthesizer
from rag.hybrid_retriever import CodeGraphRetriever, get_query_engine  

class APICombination(BaseModel):
    api_combination: List[str]
    api_combination_reason: str

class FuzzingPlanner:
    def __init__(self, llm, llm_embedding, project_name, api_info_file, api_code_file, api_call_graph_file, query_tools,  api_usage_count, code_graph_retriever: CodeGraphRetriever = None,use_memory=False):
        self.api_call_graph_file = api_call_graph_file
        self.api_code_file = api_code_file
        self.api_info_file = api_info_file
        self.project_name = project_name
        self.llm = llm
        self.llm_embedding = llm_embedding
        self.query_tools = query_tools
        self.use_memory = use_memory
        self.api_usage_count = api_usage_count
        self.code_graph_retriever = code_graph_retriever
        self.chat_memory_buffer = ChatMemoryBuffer.from_defaults(llm=llm)
        self.vector_memory = VectorMemory.from_defaults(
            vector_store=None,
            embed_model=llm_embedding,
            retriever_kwargs={"similarity_top_k": 1},
        )
        self.composable_memory = SimpleComposableMemory.from_defaults(
            primary_memory=self.chat_memory_buffer,
            secondary_memory_sources=[self.vector_memory],
        )
        self.code_summary_prompt = PromptTemplate(
            "Here is the source code information (function structure, function inputs, function return values) and the function call graph for the function named:\n"
            "{api}\n"
            "API information:\n"
            "{api_info}\n"
            "Call graph (The call graph is in CSV format, where each column represents the following attributes: 'caller', 'callee', 'caller_src', 'callee_src', 'start_body_start_line', 'start_body_end_line', 'end_body_start_line', 'end_body_end_line', 'caller_signature', 'caller_parameter_string', 'caller_return_type', 'caller_return_type_inferred', 'callee_signature', 'callee_parameter_string', 'callee_return_type', 'callee_return_type_inferred'.):\n"
            "{call_graph}\n"
            "Please generate a code summary for this function in no more than 60 words, covering the following two dimensions: code functionality and usage scenario."
        )

        self.file_summary_prompt = PromptTemplate(
            "Here is a JSON file containing all the API information from a project file:\n"
            "{file}\n"
            "with each API name followed by its code summary:\n"
            "{file_info}\n"
            "Please generate a file summary for each file in no more than 50 words, based on the code summaries of the APIs contained in each file, considering following two dimensions: file functionality and usage scenario."
            "Please translate: follow the format below: File Summary: <your summary>"
        )

        self.api_combination_query = PromptTemplate(
            "Current API usage count:\n{api_usage}\n"
            "Please provide an API combination with the following specific APIs in API usage dictionary with similar or related usage scenarios and code call relationships to this API:\n" 
            "{api}\n" 
            "The returned APIs should help achieve the highest possible code coverage when generating a fuzz driver. "
            "Prioritize APIs with lower usage counts to ensure diversity. "
            "The number of combination is limited to the maximum five APIs. "
            "Your answer should be in json format with the combination list and the reason."
        )


        self.api_combination_query_with_memory = PromptTemplate(
            "The user is working to combine different APIs from the library based on their importance and usage scenarios.\n\n"
            "Below is the historical context:\n"
            "Start\n"
            "{memory_context}\n"
            "End\n\n"
            "Current API usage count:\n{api_usage}\n"
            "Please provide an API combination with the following specific APIs in API usage dictionary with similar or related usage scenarios and code call relationships to this API:\n" 
            "{api}\n" 
            "The returned APIs should help achieve the highest possible code coverage when generating a fuzz driver. "
            "Prioritize APIs with lower usage counts to ensure diversity. "
            "The number of combination is limited to the maximum five APIs. "
            "Your answer should be in json format with the combination list and the reason."
        )


        self.mutate_api_combination_query = PromptTemplate(
            "Current API usage count (Highest Priority):\n{api_usage}\n"
            "Low coverage APIs that need more attention (Highest Priority):\n{low_coverage_apis}\n"
            "Please provide an API combination with the following specific APIs in API usage dictionary and low coverage APIs to build fuzz driver for this API:\n" 
            "{api}\n" 
            "The returned APIs should help achieve the highest possible code coverage when generating a fuzz driver. "
            "Prioritize APIs with lower usage counts to ensure diversity."
            "Also, consider including APIs from the low coverage list to improve overall coverage. "
            "The number of combination is limited to the maximum five APIs. "
            "Please note that the previous query results for {api} were {api_combine}, which did not yield an ideal coverage when generating the Fuzz driver. The results of this query should show significant changes compared to {api_combine} and ensure the highest possible coverage. "
            "The returned APIs should help achieve the highest possible code coverage when generating a fuzz driver. "
            "Your answer should be in json format with the combination list and the reason."
        
        )


        
    def set_code_graph_retriever(self, code_graph_retriever: CodeGraphRetriever):
        self.code_graph_retriever = code_graph_retriever


    def get_code_summary(self, api_info, call_graph, api):
        logger.info("User:")
        logger.info(self.code_summary_prompt.format(api=api, api_info=api_info, call_graph=call_graph))
        code_response = self.llm.complete(self.code_summary_prompt.format(api=api, api_info=api_info, call_graph=call_graph)).text
        logger.info("Assistant:")
        logger.info(code_response)
        return code_response
    

    def get_file_summary(self, file_info, file):
        logger.info("User:")
        logger.info(self.file_summary_prompt.format(file_info=file_info, file=file))
        file_response = self.llm.complete(self.file_summary_prompt.format(file_info=file_info, file=file)).text
        logger.info("Assistant:")
        logger.info(file_response)
        return file_response
    

    def update_api_usage_count(self, api_list):
        for api in api_list:
            if api not in self.api_usage_count:
                self.api_usage_count[api] = 0
            self.api_usage_count[api] += 1


    def find_call_graph_with_api(self, cg_file_path, api_name):
        data = pd.read_csv(cg_file_path)
        column1_name = 'caller'
        column2_name = 'callee'
        value_to_find = api_name
        filtered_data = []
        for index, row in data.iterrows():
            if row[column1_name] == value_to_find or row[column2_name] == value_to_find:
                filtered_data.append(row)
        return filtered_data

    def summarize_code(self):
        with open(self.api_info_file, 'r', encoding='utf-8') as f:
            existing_summaries = json.load(f)
        with open(self.api_code_file, 'r', encoding='utf-8') as f:
            api_code = json.load(f)

        for file, apis in existing_summaries.items():
            for api_name, api_sum in apis.items():
                if api_sum:
                    logger.info(f"Summary for {api_name} already exists. Skipping.")
                    continue

                logger.info(f"Generating summary for {api_name}")
                call_graph_list = self.find_call_graph_with_api(self.api_call_graph_file, api_name)
                call_graph_response = '\n'.join(' '.join(map(str, call_graph)) for call_graph in call_graph_list)
                api_info_response = api_code.get(api_name, "")
                response = self.get_code_summary(api_info_response, call_graph_response, api_name)
                existing_summaries[file][api_name] = response

            if not existing_summaries[file].get("file_summary"):
                api_dict = {file: existing_summaries[file]}
                file_info_json = json.dumps(api_dict, indent=2)
                sum_response = self.get_file_summary(file_info_json, file)
                existing_summaries[file]["file_summary"] = sum_response

        with open(self.api_info_file, "w", encoding='utf-8') as f:
            json.dump(existing_summaries, f, indent=2, sort_keys=True, ensure_ascii=False)
        logger.info(f"API summaries have been updated in {self.api_info_file}")


    def extract_api_list(self):
        try:
            with open(self.api_code_file, 'r', encoding='utf-8') as f:
                src_api_code = json.load(f)
            
            api_list = list(src_api_code.keys())
            return api_list
        except Exception as e:
            logger.error(f"Error extracting API list from source code: {str(e)}")
            return []


    def api_combination(self, api_list):
        api_combination = []

        Settings.llm = self.llm
        Settings.embed_model = self.llm_embedding

    
        combine_query_engine = get_query_engine(self.code_graph_retriever, "HYBRID", self.llm, \
                                                    get_response_synthesizer(response_mode="compact", verbose=True)
                                                        )

        response_format_program = LLMTextCompletionProgram.from_defaults(
            output_cls=APICombination,
            prompt_template_str="The input answer is {raw_answer}. Please reformat the answer with two key information, the API combination list and the reason.",
            llm=self.llm
        )

        for api in tqdm(api_list):
            question = self.api_combination_query.format(
                api=api, 
                api_list=api_list, 
                api_usage=json.dumps(self.api_usage_count)
            )
            logger.info(f"API Combination, Init Question: {question}")
            logger.info(f"Use historical context: {self.use_memory}")
            if self.use_memory:
                memory_chamessage = self.composable_memory.get(question)
                logger.info(f"Fetch historical context according to the init question: {memory_chamessage}")
                if len(memory_chamessage):
                    memory_chamessage = "\n".join([str(m) for m in memory_chamessage])
                    question = self.api_combination_query_with_memory.format(
                        api=api, 
                        memory_context=memory_chamessage, 
                        api_list=api_list,
                        api_usage=json.dumps(self.api_usage_count)
                    )
                logger.info("New question with the historical context")
                logger.info(question)
            
            response_obj = combine_query_engine.query(question)
            response_format = response_format_program(raw_answer=response_obj.response)
            response = response_format.api_combination
            logger.info(f"API Combination Response:{response_obj} {response_format}")

            query_answer = [
                ChatMessage.from_str(question, "user"),
                ChatMessage.from_str(f"{response_obj.response}", "assistant"),
            ]
            self.vector_memory.put_messages(query_answer)
            
            if response == "Empty Response":
                response = []
            response.append(api)
            api_combination.append(response)
            
            # Update API usage count
            self.update_api_usage_count(response)
            
        return api_combination
    
    def generate_single_api_combination(self, api, api_combine, low_coverage_apis):
        api_list = self.extract_api_list()

        Settings.llm=self.llm
        Settings.embed_model=self.llm_embedding
        
        combine_query_engine = get_query_engine(self.code_graph_retriever, "HYBRID", self.llm, \
                                                    get_response_synthesizer(response_mode="compact", verbose=True)
                                                        )

        response_format_program = LLMTextCompletionProgram.from_defaults(
            output_cls=APICombination,
            prompt_template_str="The input answer is {raw_answer}. Please reformat the answer with two key information, the API combination list and the reason.",
            llm=self.llm
        )

        question = self.mutate_api_combination_query.format(
            api=api, 
            api_combine=api_combine,
            low_coverage_apis=low_coverage_apis,
            api_usage=json.dumps(self.api_usage_count)
        )
        logger.info(f"API Combination, Init Question: {question}")
        logger.info(f"Use historical context: {self.use_memory}")
        if self.use_memory:
            memory_chamessage = self.composable_memory.get(question)
            logger.info(f"Fetch historical context according to the init question: {memory_chamessage}")
            if len(memory_chamessage):
                memory_chamessage = "\n".join([str(m) for m in memory_chamessage])
                question = self.api_combination_query_with_memory.format(
                    api=api, 
                    api_combine=api_combine,
                    low_coverage_apis=low_coverage_apis,
                    memory_context=memory_chamessage, 
                    api_usage=json.dumps(self.api_usage_count)
                )
            logger.info("New question with the historical context")
            logger.info(question)

        response_obj = combine_query_engine.query(question)
        response_format = response_format_program(raw_answer=response_obj.response)
        response = response_format.api_combination
        logger.info(f"API Combination Response:{response_obj} {response_format}")

        query_answer = [
            ChatMessage.from_str(question, "user"),
            ChatMessage.from_str(f"{response_obj.response}", "assistant"),
        ]
        self.vector_memory.put_messages(query_answer)

        if response == "Empty Response":
            response = []
        response.append(api)

        return response
    

   
