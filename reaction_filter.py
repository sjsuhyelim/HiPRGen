from itertools import combinations
from multiprocessing import Process, Queue
from report_generator import ReportGenerator
import sqlite3
from time import localtime, strftime
from reaction_questions import Terminal, standard_reaction_decision_tree, standard_logging_decision_tree
from constants import *

"""
Phases 3 & 4 run in paralell.

Phase 3: reaction gen and filtering
input: a bucket labeled by atom count
output: a list of reactions from that bucket
description: Loop through all possible reactions in the bucket and apply the decision tree. This will run in parallel over each bucket.

Phase 4: collating and indexing
input: all the outputs of phase 3 as they are generated
output: reaction network database
description: the worker processes from phase 3 are sending their reactions to this phase and it is writing them to DB as it gets them. We can ensure that duplicates don't get generated in phase 3 which means we don't need extra index tables on the db.

warning to contributors: be very careful when modifying the dispatcher or reaction_filter functions. They run in parallel (with one dispatcher and many reaction_filters) and it is easy to introduce subtle concurrency bugs by accident. For example, the python queues are not completely mutex locked, so the empty method can return while another processes is modifying the queue. If that doesn't make you violently shudder, then please don't tinker with the control flow of those functions. Python is not a good place to learn about parallel programming.
"""

def run_decision_tree(
        reaction,
        mol_entries,
        params,
        decision_tree,
        decision_pathway=None):
    node = decision_tree

    while type(node) == list:
        next_node = None
        for (question, new_node) in node:
            if question(reaction, mol_entries, params):

                # if decision_pathway is a list,
                # append the question which
                # answered true i.e the edge we follow
                if decision_pathway is not None:
                    decision_pathway.append(question)

                next_node = new_node
                break

        node = next_node


    if type(node) == Terminal:
        if decision_pathway is not None:
            decision_pathway.append(node)

        if node == Terminal.KEEP:
            return True
        else:
            return False
    else:
        print(node)
        raise Exception(
            """
            unexpected node type reached.
            this is usually caused because none of the questions in some node returned True.
            """)


def list_or(a_list):
    return True in a_list


create_metadata_table = """
    CREATE TABLE metadata (
            number_of_species   INTEGER NOT NULL,
            number_of_reactions INTEGER NOT NULL,
            factor_zero         REAL NOT NULL,
            factor_two          REAL NOT NULL,
            factor_duplicate    REAL NOT NULL
    );
"""

insert_metadata = """
    INSERT INTO metadata VALUES (?, ?, ?, ?, ?)
"""

create_reactions_table = """
    CREATE TABLE reactions (
            reaction_id         INTEGER NOT NULL PRIMARY KEY,
            number_of_reactants INTEGER NOT NULL,
            number_of_products  INTEGER NOT NULL,
            reactant_1          INTEGER NOT NULL,
            reactant_2          INTEGER NOT NULL,
            product_1           INTEGER NOT NULL,
            product_2           INTEGER NOT NULL,
            rate                REAL NOT NULL,
            dG                  REAL NOT NULL
    );
"""

insert_reaction = """
    INSERT INTO reactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""




def dispatcher(mol_entries,
               bucket_db,
               rn_db,
               generation_report_path,
               reaction_decision_tree=standard_reaction_decision_tree,
               logging_decision_tree=standard_logging_decision_tree,
               params={
                   'temperature' : ROOM_TEMP,
                   'electron_free_energy' : -1.4
                   },
               commit_freq=1000,
               number_of_processes=8,
               factor_zero=1.0,
               factor_two=1.0,
               factor_duplicate=1.0,
               verbose=True
               ):

    if verbose:
        print("starting reaction filtering")

    reaction_queue = Queue()
    table_queue = Queue()
    logging_queue = Queue()
    processes = {}


    if verbose:
        print("initializing report generator")

    report_generator = ReportGenerator(
        mol_entries,
        generation_report_path)



    bucket_con = sqlite3.connect(bucket_db)
    bucket_cur = bucket_con.cursor()

    res = bucket_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    for name in res:
        table = name[0]
        table_queue.put(table)

    if verbose:
        print("starting worker processes")

    for pid in range(number_of_processes):

        p = Process(
            target=reaction_filter,
            args=(
                mol_entries,
                bucket_db,
                table_queue,
                reaction_queue,
                logging_queue,
                params,
                reaction_decision_tree,
                logging_decision_tree))

        processes[pid] = p

    rn_con = sqlite3.connect(rn_db)
    rn_cur = rn_con.cursor()
    rn_cur.execute(create_metadata_table)
    rn_cur.execute(create_reactions_table)
    rn_con.commit()

    for pid in processes:
        processes[pid].start()

    living_children = True
    reaction_index = 0

    if verbose:
        print("starting inner loop")

    while ( living_children or
            not reaction_queue.empty() or
            not logging_queue.empty()):
        # if reaction queue and table queue are empty, enter a spin lock to
        # wait for spawned children to exit.
        living_bools = [processes[pid].is_alive() for pid in processes]
        living_children = list_or(living_bools)

        if not reaction_queue.empty():
            reaction = reaction_queue.get()
            rn_cur.execute(
                insert_reaction,
                (reaction_index,
                 reaction['number_of_reactants'],
                 reaction['number_of_products'],
                 reaction['reactants'][0],
                 reaction['reactants'][1],
                 reaction['products'][0],
                 reaction['products'][1],
                 reaction['rate'],
                 reaction['dG']
                 ))

            reaction_index += 1
            if reaction_index % commit_freq == 0:
                rn_con.commit()

                if verbose:
                    print(
                        '[' + strftime('%H:%M', localtime()) + ']',
                        reaction_index,
                        "reactions;",
                        table_queue.qsize(),
                        "buckets remaining")

        if not logging_queue.empty():
            reaction, decision_path = logging_queue.get()
            report_generator.emit_verbatim(
                '\n'.join([str(f) for f in decision_path]))
            report_generator.emit_reaction(reaction)
            report_generator.emit_newline()

    rn_cur.execute(
        insert_metadata,
        (len(mol_entries) + 1,
         reaction_index + 1,
        factor_zero,
        factor_two,
        factor_duplicate)
    )


    report_generator.finished()
    rn_con.commit()
    bucket_con.close()
    rn_con.close()



### filter worker

def reaction_filter(mol_entries,
                    bucket_db,
                    table_queue,
                    reaction_queue,
                    logging_queue,
                    params,
                    decision_tree,
                    logging_decision_tree):

    con = sqlite3.connect(bucket_db)
    cur = con.cursor()

    # empty is non blocking so it can return non empty while another processes is taking the last element, and get with a timeout can return empty even when the queue is not empty (if a bunch of other processes are reading from the queue and also probably other reasons)
    # To overcome this, we get the next table with a timeout and if we run out of time, then explicitly check whether the queue is empty again.
    while not table_queue.empty():

        try:
            # timeout is 1 centisecond
            table = table_queue.get(timeout=0.01)
        except:
            continue


        bucket = []
        res = cur.execute("SELECT * FROM " + table)
        for pair in res:
            bucket.append(pair)

        for (reactants, products) in combinations(bucket, 2):
            reaction = {
                'reactants' : reactants,
                'products' : products,
                'number_of_reactants' : len([i for i in reactants if i != -1]),
                'number_of_products' : len([i for i in products if i != -1])}


            reverse_reaction = {
                'reactants' : reaction['products'],
                'products' : reaction['reactants'],
                'number_of_reactants' : reaction['number_of_products'],
                'number_of_products' : reaction['number_of_reactants'],
                'reverse' : reaction
            }

            # reaction atom mapping is one of the most expensive operations we do
            # it takes ~0.02 seconds. If we compute the atom mapping for a reaction
            # we don't need to also compute if for the reverse reaction, so we couple
            # reaction / reverse pairs to facilitate that.

            # this attribute is only here for performance reasons. Question functions
            # should absolutely not be touching it unless they are about to compute
            # an atom mapping
            reaction['reverse'] = reverse_reaction

            decision_pathway_forward = []
            decision_pathway_reverse = []
            if run_decision_tree(reaction,
                                 mol_entries,
                                 params,
                                 decision_tree,
                                 decision_pathway_forward
                                 ):
                reaction_queue.put(reaction)

            if run_decision_tree(reverse_reaction,
                                 mol_entries,
                                 params,
                                 decision_tree,
                                 decision_pathway_reverse
                                 ):
                reaction_queue.put(reverse_reaction)

            if run_decision_tree(reaction,
                                 mol_entries,
                                 params,
                                 logging_decision_tree):
                logging_queue.put(
                    (reaction, decision_pathway_forward))

            if run_decision_tree(reverse_reaction,
                                 mol_entries,
                                 params,
                                 logging_decision_tree):
                logging_queue.put(
                    (reverse_reaction, decision_pathway_reverse))
