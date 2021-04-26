# %%
import os
import re
import logging
from os import getenv
from enum import Enum, auto

import sentry_sdk

import common.dialogflow_framework.stdm.dialogflow_extention as dialogflow_extention
import common.dialogflow_framework.utils.condition as condition_utils
import common.dialogflow_framework.utils.state as state_utils

import dialogflows.scopes as scopes

from common.utils import get_types_from_annotations
from common.celebrities import talk_about_celebrity, skill_trigger_phrases
from common.constants import CAN_CONTINUE_SCENARIO, MUST_CONTINUE, CAN_CONTINUE_SCENARIO_DONE
from CoBotQA.cobotqa_service import send_cobotqa

sentry_sdk.init(dsn=os.getenv("SENTRY_DSN"))

logger = logging.getLogger(__name__)

ENTITY_LINKING_URL = getenv("ENTITY_LINKING_URL")
assert ENTITY_LINKING_URL is not None


TRIGGER_PHRASES = skill_trigger_phrases() + [
    "What celebrity was the first one you really loved?",
    "Whom do you follow on Facebook?",
]

CONF_HIGH = 1
CONF_MEDIUM = 0.95
CONF_LOW = 0.75


class State(Enum):
    USR_START = auto()
    USR_FAVOURITE_CELEBRITY = auto()
    USR_ANSWERS_QUESTION = auto()
    USR_TELLS_SOMETHING = auto()
    USR_TELLS_A_FILM = auto()
    USR_YESNO_1 = auto()
    USR_YESNO_2 = auto()

    SYS_GIVE_A_FACT = auto()
    SYS_EXIT = auto()
    SYS_ASKS_A_FACT = auto()
    SYS_ASKS_A_FILM = auto()
    SYS_GOTO_CELEBRITY = auto()
    SYS_CELEBRITY_FIRST_MENTIONED = auto()
    SYS_CELEBRITY_TELL_OTHERJOBS = auto()
    SYS_TALK_ABOUT_CELEBRITY = auto()
    SYS_ACKNOWLEDGE_LINKTO_CELEBRITY = auto()
    SYS_ERR = auto()


dontwant_regex = re.compile(
    r"(not like|not want to talk|not want to hear|not concerned about|"
    r"over the |stop talking about|no more |do not watch|"
    r"not want to listen)",
    re.IGNORECASE,
)

##################################################################################################################
# Init DialogFlow
##################################################################################################################


simplified_dialogflow = dialogflow_extention.DFEasyFilling(State.USR_START)


##################################################################################################################
##################################################################################################################
# Design DialogFlow.
##################################################################################################################
##################################################################################################################
##################################################################################################################
# utils
##################################################################################################################
# ....
##################################################################################################################
# std greeting
##################################################################################################################


def default_condition_request(ngrams, vars):
    flag = True
    flag = flag and not condition_utils.is_switch_topic(vars)
    flag = flag and not condition_utils.is_lets_chat_about_topic_human_initiative(vars)
    flag = flag and not condition_utils.is_question(vars)
    flag = flag or talk_about_celebrity_request(ngrams, vars)
    logger.info(f"default_condition_request {flag}")
    return flag


def yes_request(ngrams, vars):
    flag = condition_utils.is_yes_vars(vars)
    logger.info(f"yes_request: {flag}")
    return flag


def yes_actor_request(ngrams, vars):
    flag = condition_utils.is_yes_vars(vars)
    shared_memory = state_utils.get_shared_memory(vars)
    is_actor = shared_memory.get("actor", False)
    flag = flag and is_actor
    return flag


def no_request(ngrams, vars):
    flag = condition_utils.is_no_vars(vars)
    logger.info(f"no_request: {flag}")
    return flag


def dont_want_request(ngrams, vars):
    human_utterance_text = state_utils.get_last_human_utterance(vars)["text"].lower()
    flag = bool(re.search(dontwant_regex, human_utterance_text))
    logger.info(f"dont_want_request: {flag}")
    return flag


def talk_about_celebrity_request(ngrams, vars):
    human_utterance = state_utils.get_last_human_utterance(vars)
    bot_utterance = state_utils.get_last_bot_utterance(vars)
    flag = talk_about_celebrity(human_utterance, bot_utterance)
    logger.info(f"talk_about_celebrity_request: {flag}")
    return flag


def give_fact_request(ngrams, vars):
    bot_utterance = state_utils.get_last_bot_utterance(vars)
    flag = all(
        [
            bot_utterance["active_skill"] == "celebrity_skill",
            condition_utils.is_yes_vars(vars),
            celebrity_in_any_phrase_request(ngrams, vars),
        ]
    )
    logger.info(f"give_fact: {flag}")
    return flag


def celebrity_in_phrase_request(ngrams, vars, use_only_last_utt=True):
    shared_memory = state_utils.get_shared_memory(vars)
    asked_celebrities = shared_memory.get("asked_celebrities", [])
    celebrity = get_celebrity(vars, use_only_last_utt=use_only_last_utt)[0]
    flag = celebrity and celebrity not in asked_celebrities
    logger.info(f"celebrity_in_phrase_request : {flag}")
    return flag


def celebrity_in_any_phrase_request(ngrams, vars):
    flag = celebrity_in_phrase_request(ngrams, vars, use_only_last_utt=False)
    logger.info(f"celebrity_in_any_phrase_request : {flag}")
    return flag


def get_cobot_fact(celebrity_name, given_facts):
    logger.debug(f"Calling cobot_fact for {celebrity_name} {given_facts}")
    answer = send_cobotqa(f"fact about {celebrity_name}")
    if answer is None:
        error_message = f"Answer from cobotqa or fact about {celebrity_name} not obtained"
        logger.error(error_message)
        sentry_sdk.capture_exception(Exception(error_message))
        return None
    for phrase_ in ["This might answer your question", "According to Wikipedia"]:
        if phrase_ in answer:
            answer = answer.split(phrase_)[1]
    logger.debug(f"Answer from cobot_fact obtained {answer}")
    if answer not in given_facts:
        return answer
    else:
        return ""


def filter_occupations(last_wp, tocheck_relation="occupation"):
    wp_annotations = last_wp.get("wiki_parser", {})
    if isinstance(wp_annotations, list) and wp_annotations:  # support 2 different formats
        wp_annotations = wp_annotations[0]
    if "topic_skill_entities_info" in wp_annotations:
        for entity in wp_annotations["topic_skill_entities_info"]:
            entity_dict = wp_annotations["topic_skill_entities_info"][entity].copy()
            for relation in entity_dict:
                if relation != tocheck_relation:
                    del wp_annotations["topic_skill_entities_info"][entity][relation]
    return wp_annotations


def get_celebrity(vars, exclude_types=False, use_only_last_utt=False):
    # if 'agent' not in vars:
    #     vars = {'agent': vars}
    shared_memory = state_utils.get_shared_memory(vars)
    last_wp = shared_memory.get("last_wp", {"wiki_parser": {}})
    # dialog = vars["agent"]['dialog']
    human_utterance = state_utils.get_last_human_utterance(vars)
    logger.debug(f'Calling get_celebrity on {human_utterance["text"]} {exclude_types} {use_only_last_utt}')
    raw_profession_list = [
        "Q33999",  # actor
        "Q10800557",  # film actor
        "Q10798782",  # television actor
        "Q2405480",  # voice actor
        "Q17125263",  # youtuber
        "Q245068",  # comedian
        "Q19204627",  # American football player
        "Q2066131",  # sportsman
        "Q947873",  # television presenter
        "Q2405480",  # comedian
        "Q211236",  # celebrity
        "Q177220",  # singer
        "Q82955"  # politician
    ]
    actor_profession_list = raw_profession_list[:4]
    mentioned_otherjobs = shared_memory.get("mentioned_otherjobs", [])
    if exclude_types:
        raw_profession_list = raw_profession_list + mentioned_otherjobs
    celebrity_name, celebrity_type, celebrity_raw_type = get_types_from_annotations(
        human_utterance["annotations"],
        tocheck_relation="occupation",
        types=raw_profession_list,
        exclude_types=exclude_types,
    )
    met_actor = celebrity_raw_type in actor_profession_list
    if not celebrity_name:
        celebrity_name, celebrity_type, celebrity_raw_type = get_types_from_annotations(
            last_wp, tocheck_relation="occupation", types=raw_profession_list, exclude_types=exclude_types
        )
    else:
        # we found in last utterance celeb name
        last_wp = {"wiki_parser": human_utterance["annotations"].get("wiki_parser", {})}
        last_wp = filter_occupations(last_wp)
    if exclude_types:
        mentioned_otherjobs = mentioned_otherjobs + [celebrity_raw_type]
    logger.debug(f"Answer for get_celebrity exclude_types {exclude_types} : {celebrity_name} {celebrity_type}")
    state_utils.save_to_shared_memory(vars, last_wp=last_wp, mentioned_otherjobs=mentioned_otherjobs, actor=met_actor)
    return celebrity_name, celebrity_type


def propose_celebrity_response(vars):
    try:
        shared_memory = state_utils.get_shared_memory(vars)
        asked_celebrities = shared_memory.get("asked_celebrities", [])
        bot_utterance = state_utils.get_last_bot_utterance(vars)
        confidence = CONF_LOW
        if any([trigger_phrase in bot_utterance["text"] for trigger_phrase in TRIGGER_PHRASES]):
            confidence = CONF_HIGH
        state_utils.set_confidence(vars, confidence=confidence)
        state_utils.set_can_continue(vars, continue_flag=CAN_CONTINUE_SCENARIO)
        celebrity, celebrity_name = get_celebrity(vars)
        asked_celebrities = asked_celebrities + [celebrity]
        state_utils.save_to_shared_memory(vars, asked_celebrities=asked_celebrities)
        if celebrity and celebrity_name:
            answer = f"{celebrity} is an amazing {celebrity_name} ! May I tell you something about this person?"
            if celebrity_name == 'politician':
                answer = answer.replace('an amazing', 'a')  # We are politically neutral
            return answer
        else:
            msg = "No return value in get_celebrity when it should"
            logger.warn(msg)
            sentry_sdk.capture_message(msg)
            return error_response(vars)
    except Exception as exc:
        return error_response(vars, exc)


def celebrity_fact_response(vars):
    try:
        state_utils.set_confidence(vars, confidence=CONF_HIGH)
        state_utils.set_can_continue(vars, continue_flag=MUST_CONTINUE)
        logger.debug("Getting celebrity facts")
        celebrity_name, celebrity_type = get_celebrity(vars)
        shared_memory = state_utils.get_shared_memory(vars)
        given_facts = shared_memory.get("given_facts", [])
        logger.debug(f"Given facts in memory {given_facts}")
        num_attempts = 2
        curr_fact = ""
        next_fact = shared_memory.get("next_fact", "")
        for _ in range(num_attempts):
            if next_fact:
                curr_fact = next_fact
                next_fact = ""
            if not curr_fact:
                curr_fact = get_cobot_fact(celebrity_name, given_facts)
            if not next_fact:
                next_fact = get_cobot_fact(celebrity_name, given_facts + [curr_fact])
        if not next_fact:
            celebrity_name, celebrity_otherjob = get_celebrity(vars, exclude_types=True)
            next_fact = f"{celebrity_name} is also a {celebrity_otherjob}."
        logger.debug(f"Curr {curr_fact} next {next_fact}")
        if curr_fact:
            reply = f"{curr_fact}"
            if next_fact:
                reply = f"{reply} May I tell you another fact about this {celebrity_type}?"
            state_utils.save_to_shared_memory(vars, given_facts=given_facts + [curr_fact], next_fact=next_fact)
            logger.debug(f"In function celebrity_fact_response answer {reply}")
            return reply
        else:
            msg = "We should have proposed a fact here"
            logger.warn(msg)
            sentry_sdk.capture_message(msg)
            return error_response(vars)
    except Exception as exc:
        return error_response(vars, exc)


def celebrity_otherjob_response(vars):
    try:
        state_utils.set_confidence(vars, confidence=CONF_HIGH)
        state_utils.set_can_continue(vars, continue_flag=MUST_CONTINUE)
        celebrity_name, celebrity_otherjob = get_celebrity(vars, exclude_types=True)
        if celebrity_otherjob and celebrity_name:
            shared_memory = state_utils.get_shared_memory(vars)
            given_facts = shared_memory.get("given_facts", [])
            next_fact = shared_memory.get("next_fact", "")
            if not next_fact:
                next_fact = get_cobot_fact(celebrity_name, given_facts)
                state_utils.save_to_shared_memory(vars, next_fact=next_fact)
            reply = f"{celebrity_name} is also a {celebrity_otherjob}. "
            if next_fact:
                reply = f"{reply} May I tell you something else about this person?"
            state_utils.set_confidence(vars, confidence=CONF_HIGH)
            state_utils.set_can_continue(vars)
            return reply
        else:
            msg = "We should have found other job here"
            logger.warn(msg)
            sentry_sdk.capture_message(msg)
            return error_response(vars)
    except Exception as exc:
        return error_response(vars, exc)
    return error_response(vars)


def info_response(vars):
    state_utils.set_confidence(vars, confidence=CONF_MEDIUM)
    state_utils.set_can_continue(vars)
    return "Could you please tell me more about this person?"


def acknowledge_and_link_to_celebrity_response(vars):
    state_utils.set_confidence(vars, confidence=CONF_MEDIUM)
    state_utils.set_can_continue(vars, continue_flag=CAN_CONTINUE_SCENARIO_DONE)
    return f"Sounds interesting. But let's talk about something else. {favourite_celebrity_response(vars)}"


def link_to_celebrity_response(vars):
    return f"OK. {favourite_celebrity_response(vars)}"


def ask_film_response(vars):
    state_utils.set_confidence(vars, confidence=CONF_MEDIUM)
    state_utils.set_can_continue(vars, CAN_CONTINUE_SCENARIO_DONE)
    return "What is your favourite film with this actor?"


def favourite_celebrity_response(vars):
    try:
        celebrity_questions = TRIGGER_PHRASES
        confidences = [CONF_HIGH, CONF_MEDIUM, CONF_LOW]  # we becone less confident by the flow
        shared_memory = state_utils.get_shared_memory(vars)
        asked_questions = shared_memory.get("asked_questions", [])
        for i, celebrity_question in enumerate(celebrity_questions):
            if celebrity_question not in asked_questions:
                state_utils.save_to_shared_memory(
                    vars,
                    asked_questions=asked_questions + [celebrity_question],
                    last_wp={"wiki_parser": {}},
                    mentioned_otherjobs=[],
                )
                confidence = confidences[i]
                state_utils.set_confidence(vars, confidence=confidence)
                if i == 0:
                    state_utils.set_can_continue(vars, continue_flag=CAN_CONTINUE_SCENARIO)
                else:
                    state_utils.set_can_continue(vars, continue_flag=CAN_CONTINUE_SCENARIO_DONE)
                return celebrity_question
    except Exception as exc:
        return error_response(vars, exc)
    return error_response(vars)


##################################################################################################################
# error
##################################################################################################################


def error_response(vars, exc=None):
    if exc is not None:
        logger.exception(exc)
        sentry_sdk.capture_exception(exc)
    state_utils.set_confidence(vars, 0)
    return ""


##################################################################################################################
##################################################################################################################
# linking
##################################################################################################################
##################################################################################################################


##################################################################################################################
#  START
# TO ADD TRANSITIONS!!!!
simplified_dialogflow.add_user_serial_transitions(
    State.USR_START,
    {
        State.SYS_CELEBRITY_FIRST_MENTIONED: celebrity_in_phrase_request,
        State.SYS_TALK_ABOUT_CELEBRITY: talk_about_celebrity_request,
    },
)

simplified_dialogflow.add_system_transition(
    State.SYS_TALK_ABOUT_CELEBRITY, State.USR_FAVOURITE_CELEBRITY, favourite_celebrity_response
)
simplified_dialogflow.add_system_transition(
    State.SYS_CELEBRITY_FIRST_MENTIONED, State.USR_YESNO_1, propose_celebrity_response
)
simplified_dialogflow.add_user_serial_transitions(
    State.USR_FAVOURITE_CELEBRITY,
    {
        State.SYS_CELEBRITY_FIRST_MENTIONED: celebrity_in_any_phrase_request,
        State.SYS_EXIT: dont_want_request,
        State.SYS_ASKS_A_FACT: default_condition_request,
    },
)

simplified_dialogflow.add_system_transition(State.SYS_ASKS_A_FACT, State.USR_TELLS_SOMETHING, info_response)

simplified_dialogflow.add_user_serial_transitions(
    State.USR_ANSWERS_QUESTION,
    {State.SYS_EXIT: dont_want_request, State.SYS_ACKNOWLEDGE_LINKTO_CELEBRITY: default_condition_request},
)

simplified_dialogflow.add_system_transition(
    State.SYS_ACKNOWLEDGE_LINKTO_CELEBRITY, State.USR_FAVOURITE_CELEBRITY, acknowledge_and_link_to_celebrity_response
)

simplified_dialogflow.add_user_serial_transitions(
    State.USR_YESNO_1,
    {
        State.SYS_CELEBRITY_TELL_OTHERJOBS: yes_request,  # function calling
        State.SYS_EXIT: dont_want_request,
        State.SYS_GOTO_CELEBRITY: no_request,
    },
)
##################################################################################################################
#  SYS_HI

simplified_dialogflow.add_system_transition(
    State.SYS_CELEBRITY_TELL_OTHERJOBS, State.USR_YESNO_2, celebrity_otherjob_response
)
simplified_dialogflow.add_system_transition(
    State.SYS_GOTO_CELEBRITY, State.USR_FAVOURITE_CELEBRITY, link_to_celebrity_response
)
simplified_dialogflow.add_user_serial_transitions(
    State.USR_YESNO_2,
    {
        State.SYS_ASKS_A_FILM: yes_actor_request,
        State.SYS_GIVE_A_FACT: yes_request,
        State.SYS_EXIT: dont_want_request,
        State.SYS_GOTO_CELEBRITY: no_request,
    },
)
simplified_dialogflow.add_system_transition(State.SYS_GIVE_A_FACT, State.USR_YESNO_2, celebrity_fact_response)
simplified_dialogflow.add_system_transition(State.SYS_ASKS_A_FILM, State.USR_TELLS_A_FILM, ask_film_response)

for state_ in State:
    simplified_dialogflow.set_error_successor(state_, State.SYS_ERR)

##################################################################################################################
#  SYS_ERR
simplified_dialogflow.add_system_transition(
    State.SYS_ERR,
    (scopes.MAIN, scopes.State.USR_ROOT),
    error_response,
)
##################################################################################################################
#  Compile and get dialogflow
##################################################################################################################
# do not foget this line
dialogflow = simplified_dialogflow.get_dialogflow()
