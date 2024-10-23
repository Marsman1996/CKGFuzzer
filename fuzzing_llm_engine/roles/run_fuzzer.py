
from configs.llm_config import LLMConfig
# from models.baseLLM import BaseLLM
import os
from utils.check_gen_fuzzer import run
from repo.coverage_postproc import *
from .compilation_fix_agent import CompilationFixAgent, extract_code
from .fuzz_generator import FuzzingGenerationAgent
from .input_gen_agent import InputGenerationAgent
from .crash_analyzer import CrashAnalyzer
from .planner import FuzzingPlanner
from loguru import logger
# import shutil
# import re
from datetime import datetime
import textwrap

import zipfile
import yaml
import json
import time
import csv




def save_crash_analysis(fuzz_project_dir, fuzz_driver_file, is_api_bug, crash_category, crash_analysis, crash_info, fuzz_driver_path):
    crash_dir = os.path.join(fuzz_project_dir, "crash")
    if not os.path.exists(crash_dir):
        os.makedirs(crash_dir)
    
    # Create a subdirectory for this fuzz driver
    fuzz_driver_name = os.path.splitext(fuzz_driver_file)[0]
    fuzz_driver_crash_dir = os.path.join(crash_dir, fuzz_driver_name)
    if not os.path.exists(fuzz_driver_crash_dir):
        os.makedirs(fuzz_driver_crash_dir)
    
    yaml_file_path = os.path.join(fuzz_driver_crash_dir, "crash_analysis.yaml")
    
    # Load existing data if file exists
    if os.path.exists(yaml_file_path):
        with open(yaml_file_path, 'r') as f:
            crash_data = yaml.safe_load(f) or []
    else:
        crash_data = []

    # Create a unique identifier for this crash
    crash_id = f"crash_{len(crash_data) + 1}"

    # Save the fuzz driver file with a unique name
    fuzz_driver_dest = os.path.join(fuzz_driver_crash_dir, f"{fuzz_driver_name}_{crash_id}.cc")
    shutil.copy2(fuzz_driver_path, fuzz_driver_dest)

    # Prepare the crash analysis with proper indentation
    formatted_crash_analysis = textwrap.indent(crash_analysis.strip(), '      ')
    formatted_crash_info = textwrap.indent(crash_info.strip(), '      ')

    # Append new crash analysis
    crash_data.append({
        crash_id: {
            "is_api_bug": is_api_bug,
            "crash_category": crash_category,
            "crash_analysis": f"|\n{formatted_crash_analysis}",
            "crash_info": f"|\n{formatted_crash_info}",
            "fuzz_driver_file": os.path.basename(fuzz_driver_dest),
            "timestamp": datetime.now().isoformat()
        }
    })
    
    # Custom YAML dumper to preserve multi-line strings
    class literal_str(str):
        pass

    def literal_presenter(dumper, data):
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')

    yaml.add_representer(literal_str, literal_presenter)

    # Convert multi-line strings to literal_str
    for crash in crash_data:
        for key, value in crash[list(crash.keys())[0]].items():
            if isinstance(value, str) and '\n' in value:
                crash[list(crash.keys())[0]][key] = literal_str(value)

    # Save the updated data back to the file
    with open(yaml_file_path, 'w') as f:
        yaml.dump(crash_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    
    logger.info(f"New crash analysis (ID: {crash_id}) for {fuzz_driver_file} appended to {yaml_file_path}")
    logger.info(f"Fuzz driver file saved as {fuzz_driver_dest}")


class Fuzzer():
    def __init__(self, directory,project, fuzz_project_dir, corpus_dir, coverage_dir,report_dir,time_budget,report_target_dir,planner:FuzzingPlanner, fuzz_gen:FuzzingGenerationAgent,compilation_fix_agent:CompilationFixAgent,input_gen_agent:InputGenerationAgent, crash_analyzer:CrashAnalyzer, api_usage_count, max_itr_fuzz_loop=3):
        self.directory = directory
        self.project = project
        self.fuzz_project_dir = fuzz_project_dir
        self.output_dir = corpus_dir
        self.coverage_dir = coverage_dir
        self.report_dir = report_dir
        self.time_budget = time_budget
        self.covered_lines = 0
        self.covered_branches = 0
        self.max_itr_fuzz_loop= max_itr_fuzz_loop
        self.compilation_fix_agent = compilation_fix_agent
        self.planner = planner
        self.fuzz_gen = fuzz_gen
        self.input_gen = input_gen_agent
        self.crash_analyzer = crash_analyzer
        self.api_usage_count = api_usage_count
        self.failed_builds = []

        self.report_target_dir = report_target_dir


    def set_api_combination(self, api_combination):
        self.api_combination = api_combination

    def set_api_code(self, api_code):
        self.api_code = api_code

    def set_api_summary(self, api_summary):
        self.api_summary = api_summary
    
    def set_fuzz_gen_code_output_dir(self, fuzz_gen_code_output_dir):
        self.fuzz_gen_code_output_dir = fuzz_gen_code_output_dir

    
    def update_api_usage_count(self, api_combination):
        for api in api_combination:
            if api in self.api_usage_count:
                self.api_usage_count[api] += 1
            else:
                self.api_usage_count[api] = 1
        
        logger.info(f"Updated API usage count: {self.api_usage_count}")

    
    def analyze_low_coverage_files(self, threshold,file_coverages):
        merge_dir = os.path.join(self.report_dir, "merge_report")
        api_summary_path = os.path.join(self.fuzz_project_dir, "api_summary", "api_with_summary.json")

        if not os.path.exists(merge_dir):
            logger.warning(f"Merge directory does not exist: {merge_dir}")
            return []

        if not os.path.exists(api_summary_path):
            logger.warning(f"API summary file does not exist: {api_summary_path}")
            return []


        if not file_coverages:
            logger.warning("No coverage data found.")
            return []

        sorted_coverages, low_coverage_files = sort_and_filter_coverages(file_coverages, threshold)

        with open(api_summary_path, 'r') as f:
            api_summary = json.load(f)
   
        low_coverage_apis = []
        for file in low_coverage_files:
            file_name = file.split('.')[0] + '.c'  
            if file_name in api_summary:
                apis = [api for api in api_summary[file_name] if api != 'file_summary']
                low_coverage_apis.extend(apis)

        return low_coverage_apis


    def build_and_fuzz_one_file(self, fuzz_driver_file, fix_fuzz_driver_dir=None):
        if fix_fuzz_driver_dir is None:
            fix_fuzz_driver_dir = os.path.join(self.directory, f"fuzz_driver/{self.project}/compilation_pass_rag/")
        if not os.path.exists(fix_fuzz_driver_dir):
            logger.info(f"No folder {fix_fuzz_driver_dir}")
            return 
        fuzzer_name, _ = os.path.splitext(fuzz_driver_file)

        # Extract the number from fuzzer_name
        fuzzer_number = None
        parts = fuzzer_name.split('_')
        if len(parts) >= 4:
            fuzzer_number = int(parts[-1].split('.')[0])
        api_combine=self.api_combination[fuzzer_number-1]
        api_name=api_combine[-1]
        logger.info(f"Current Fuzzing API Name: {api_name}, its combination: {api_combine}")
        
        # build fuzz driver    
        run_args = ["build_fuzzer_file",self.project, "--fuzz_driver_file", fuzz_driver_file]    
        build_fuzzer_result =  run(run_args) 
        logger.info(f"compile {fuzz_driver_file}, result {build_fuzzer_result}")
        self.save_coverage() 
        # Check if the build was successful
        if "ERROR" in build_fuzzer_result or "error" in build_fuzzer_result.lower():
            logger.error(f"Failed to build fuzzer {fuzz_driver_file}. Skipping this file.")
            self.failed_builds.append(fuzz_driver_file)
            return
        else:
        # If we've reached this point, the build was successful
            logger.info(f"Successfully built fuzzer {fuzz_driver_file}")
            corpus_dir = os.path.join(self.output_dir, f'{fuzzer_name}_corpus')
            if not os.path.isdir(corpus_dir):
                # empty corpus
                os.makedirs(corpus_dir, exist_ok=True)
            # run fuzzer with libfuzzer
            run_args = ["run_fuzzer", self.project,"--timeout", self.time_budget, "--fuzz_driver_file", fuzz_driver_file, fuzzer_name,"--fuzzing_llm_dir", self.directory,"--corpus-dir",f"{corpus_dir}"]  
            run_fuzzer_result =  run(run_args)  
            logger.info(f"run_fuzzer {fuzz_driver_file}, result {run_fuzzer_result}")

            if "ERROR" in run_fuzzer_result:
                logger.info("Crash detected. Analyzing...")
                error_index = run_fuzzer_result.index("ERROR")
                crash_info = run_fuzzer_result[error_index:]  # Extract only the error message
                is_api_bug, crash_category, crash_analysis = self.crash_analyzer.analyze_crash(crash_info, f"{fix_fuzz_driver_dir}/{fuzz_driver_file}", api_combine)
                save_crash_analysis(self.fuzz_project_dir, fuzz_driver_file, is_api_bug, crash_category, crash_analysis, crash_info,f"{fix_fuzz_driver_dir}/{fuzz_driver_file}")
            

            # build fuzzer with coverage to collect the coverage reports
            run_args=['build_fuzzers',self.project, "--sanitizer", "coverage", "--fuzzing_llm_dir", self.directory, "--fuzz_driver_file", fuzz_driver_file]
            build_fuzzers_result =  run(run_args)  
            logger.info(f"build coverage {self.project}, result {build_fuzzers_result}")
            
            # compute coverage
            run_args=['coverage', self.project, "--fuzz-target",fuzzer_name, "--fuzz_driver_file", fuzz_driver_file,"--corpus-dir", f"{corpus_dir}", "--fuzzing_llm_dir", self.directory,"--no_serve"]
            coverage_result =  run(run_args)  
            logger.info(f"coverage {fuzz_driver_file}, result {coverage_result}")

            logger.info("Computing current coverage...")
            # collect coverage reports
            html2txt(f"{self.coverage_dir}{fuzzer_name}{self.report_target_dir}",f"{self.report_dir}{fuzzer_name}/")  

            update_successful, current_line_coverage, total_lines, covered_lines, current_branch_coverage, total_branches, covered_branches, file_coverages = update_coverage_report(
                f"{self.report_dir}merge_report",
                f"{self.report_dir}{fuzzer_name}/"
            )
            
            if self.covered_lines==0 and self.covered_branches==0:
                self.covered_lines = covered_lines
                self.covered_branches = covered_branches
                logger.info(f"Current covered lines: {self.covered_lines}, Total lines: {total_lines}")
                logger.info(f"Current covered branches: {self.covered_branches}, Total branches: {total_branches}")
                self.save_coverage() 
                self.update_api_usage_count(api_combine)

            else:
                if update_successful:
                    self.covered_lines = covered_lines
                    self.covered_branches = covered_branches
                    logger.info(f"Coverage updated. Current covered lines: {self.covered_lines}, Total lines: {total_lines}")
                    logger.info(f"Current covered branches: {self.covered_branches}, Total branches: {total_branches}")
                    self.save_coverage() 
                    self.update_api_usage_count(api_combine)
                else:   
                    i=0
                    while not update_successful and i < self.max_itr_fuzz_loop:
                        if i == 0:
                            current_api_combine = api_combine
                        else:
                            current_api_combine = new_api_combine
                        logger.info(f"No new branches covered. Regenerating API combination. Iteration: {i+1}")
                        
                        low_coverage_apis = self.analyze_low_coverage_files(current_branch_coverage,file_coverages)
                        logger.info(f"Low coverage APIs: {low_coverage_apis}")
     
                        new_api_combine = self.planner.generate_single_api_combination(api_name, current_api_combine, low_coverage_apis,)
                        logger.info(f"New API Combination: {new_api_combine}")
                        
                        self.fuzz_gen.generate_single_fuzz_driver(new_api_combine, fuzz_driver_file, self.api_code, self.api_summary, self.fuzz_gen_code_output_dir)
                        compilation_success = self.compilation_fix_agent.single_fix_compilation(fuzz_driver_file, self.fuzz_gen_code_output_dir, self.project)
                        
                        if not compilation_success:
                            logger.info(f"Compilation check failed after max iterations, continue to fuzzing")
                            os.remove(f"{fix_fuzz_driver_dir}/{fuzz_driver_file}")
                            return
                    
                        self.input_gen.generate_input_fuzz_driver(f"{fix_fuzz_driver_dir}/{fuzz_driver_file}")

                        run_args = ["build_fuzzer_file",self.project, "--fuzz_driver_file", fuzz_driver_file]    
                        build_fuzzer_result =  run(run_args) 
                        logger.info(f"compile {fuzz_driver_file}, result {build_fuzzer_result}")

                        if "ERROR" in build_fuzzer_result or "error" in build_fuzzer_result.lower():
                            logger.error(f"Failed to build fuzzer {fuzz_driver_file}. Skipping this file.")
                            continue

                        run_args = ["run_fuzzer", self.project,"--timeout", self.time_budget, "--fuzz_driver_file", fuzz_driver_file, fuzzer_name,"--fuzzing_llm_dir", self.directory,"--corpus-dir",f"{corpus_dir}"]  
                        run_fuzzer_result =  run(run_args)  
                        logger.info(f"run_fuzzer {fuzz_driver_file}, result {run_fuzzer_result}")

                        self.save_coverage() 

                        if "ERROR" in run_fuzzer_result:
                            logger.info("Crash detected. Analyzing...")
                            error_index = run_fuzzer_result.index("ERROR")
                            crash_info = run_fuzzer_result[error_index:]  
                            is_api_bug, crash_category, crash_analysis = self.crash_analyzer.analyze_crash(crash_info, f"{fix_fuzz_driver_dir}/{fuzz_driver_file}", current_api_combine)
                            save_crash_analysis(self.fuzz_project_dir, fuzz_driver_file, is_api_bug, crash_category, crash_analysis, crash_info,f"{fix_fuzz_driver_dir}/{fuzz_driver_file}")
                      
                        # build fuzzer with coverage to collect the coverage reports
                        run_args=['build_fuzzers',self.project, "--sanitizer", "coverage", "--fuzzing_llm_dir", self.directory, "--fuzz_driver_file", fuzz_driver_file]
                        build_fuzzers_result =  run(run_args)  
                        logger.info(f"build coverage {self.project}, result {build_fuzzers_result}")  

                        run_args=['coverage',self.project, "--fuzz-target",fuzzer_name, "--fuzz_driver_file", fuzz_driver_file,"--corpus-dir", f"{corpus_dir}", "--fuzzing_llm_dir", self.directory,"--no_serve"]
                        coverage_result =  run(run_args)  
                        logger.info(f"coverage {fuzz_driver_file}, result {coverage_result}")


                        html2txt(f"{self.coverage_dir}{fuzzer_name}{self.report_target_dir}",f"{self.report_dir}{fuzzer_name}/")
                        update_successful, current_line_coverage, total_lines, covered_lines,current_branch_coverage, total_branches, covered_branches, file_coverages = update_coverage_report(
                            f"{self.report_dir}merge_report",
                            f"{self.report_dir}{fuzzer_name}/"
                            )
                        if update_successful:
                            self.covered_lines = covered_lines
                            self.covered_branches = covered_branches
                            logger.info(f"Coverage updated. Current covered lines: {self.covered_lines}, Total lines: {total_lines}")
                            logger.info(f"Current covered branches: {self.covered_branches}, Total branches: {total_branches}")
                            self.save_coverage() 
                            self.planner.update_api_usage_count(new_api_combine)
                            
                            # Update api_combine with new_api_combine
                            self.api_combination[fuzzer_number-1] = new_api_combine
                            
                            # Save updated api_combine to JSON file
                            json_file_path = self.fuzz_project_dir+"agents_results/api_combine.json"
                            with open(json_file_path, 'w') as f:
                                json.dump(self.api_combination, f, indent=2)
                            
                            logger.info(f"Updated api_combine saved to {json_file_path}")
                            return
                        i += 1
                    # Update api_combine with new_api_combine even if max iterations reached
                    self.api_combination[fuzzer_number-1] = new_api_combine
                    
                    # Save updated api_combine to JSON file
                    json_file_path = self.fuzz_project_dir+"agents_results/api_combine.json"
                    with open(json_file_path, 'w') as f:
                        json.dump(self.api_combination, f, indent=2)
                    self.planner.update_api_usage_count(new_api_combine)
                    
                    logger.info(f"Updated api_combine saved to {json_file_path} after reaching max iterations")

                    logger.info("Max iterations reached.")
                    logger.info(f"Coverage updated. Current covered lines: {self.covered_lines}, Total lines: {total_lines}")
                    logger.info(f"Current covered branches: {self.covered_branches}, Total branches: {total_branches}")
                    self.save_coverage() 

    
    
    def build_and_fuzz(self):
        fix_fuzz_driver_dir = os.path.join(self.directory, f"fuzz_driver/{self.project}/compilation_pass_rag/")
        if not os.path.exists(fix_fuzz_driver_dir):
            logger.info(f"No folder {fix_fuzz_driver_dir}")
            return 
        # build_fuzzer_file
        logger.info(os.listdir(fix_fuzz_driver_dir))
        # Check and remove the merge_report directory if it exists
        merge_report_path = f"{self.report_dir}merge_report"
        if os.path.exists(merge_report_path):
            logger.info(f"Removing existing merge_report directory: {merge_report_path}")
            shutil.rmtree(merge_report_path)
        for fuzz_driver_file in os.listdir(fix_fuzz_driver_dir):  
            logger.info(f"Fuzz Driver File {fuzz_driver_file}")
            self.build_and_fuzz_one_file(fuzz_driver_file, fix_fuzz_driver_dir=fix_fuzz_driver_dir)
            self.save_coverage()
        logger.info(f"Failed builds: {self.failed_builds}")


  
        