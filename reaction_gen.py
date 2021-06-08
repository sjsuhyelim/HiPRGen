from mol_entry import MoleculeEntry
from itertools import combinations
from functools import partial
from multiprocessing import Process, Queue
from enum import Enum
from constants import *
import sqlite3
import math

"""
Phases 3 & 4 run in paralell.

Phase 3: reaction gen and filtering
input: a bucket labeled by atom count
output: a list of reactions from that bucket
description: Loop through all possible reactions in the bucket and apply the decision tree. This will run in parallel over each bucket.

The reaction decision tree:

A question is a function q(reaction, mol_entries, params) -> Bool

reaction is a dict:

        reaction = {
            'reactants' : reactant indices
            'products' : product indices,
            'number_of_reactants',
            'number_of_products'}

params is a dict:


        params = {
           'temperature'
           'electron_free_energy'
        }

The lists of reactant and product indices always have length two. We use -1 when there is a only a single reactant or product.

The questions can also set reaction['rate'] and reaction['dG']

Questions will be writable by hand, or we could have machine learning filters.

A node is either a Terminal or a non empty list [(question, node)]

class Terminal(Enum):
    KEEP = 1
    DISCARD = -1

For the return value of a question, True means travel to this node and False means try next question in the list.

for non terminal nodes, it is an error if every question returns False. i.e getting stuck at a non terminal node is an error.

Once a Terminal node is reached, it tells us whether to keep or discard the reaction.


Phase 4: collating and indexing
input: all the outputs of phase 3 as they are generated
output: reaction network database
description: the worker processes from phase 3 are sending their reactions to this phase and it is writing them to DB as it gets them. We can ensure that duplicates don't get generated in phase 3 which means we don't need extra index tables on the db.
"""



### decision tree

class Terminal(Enum):
    KEEP = 1
    DISCARD = -1

def run_decision_tree(reaction, mol_entries, params, decision_tree):
    node = decision_tree

    while type(node) == list:
        next_node = None
        for (question, new_node) in node:
            if question(reaction, mol_entries, params):
                next_node = new_node
                break

        node = next_node


    if type(node) == Terminal:
        if node == Terminal.KEEP:
            return True
        else:
            return False
    else:
        print(node)
        raise Exception("unexpected node type reached")

def default_rate(dG, params):
    kT = KB * params['temperature']
    max_rate = kT / PLANCK

    if dG < 0:
        rate = max_rate
    else:
        rate = max_rate * math.exp(- dG / kT)

    return rate

def dG_above_threshold(threshold, reaction, mol_entries, params):
    dG = 0.0

    for index in reaction['reactants']:
        if index != -1:
            dG -= mol_entries[index].get_free_energy()

    for index in reaction['products']:
        if index != -1:
            dG += mol_entries[index].get_free_energy()

    if dG > threshold:
        return True
    else:
        reaction['dG'] = dG
        reaction['rate'] = default_rate(dG, params)
        return False

def default_true(reaction, mols, params):
    return True

standard_decision_tree = [
    (partial(dG_above_threshold, 0.5), Terminal.DISCARD),
    (default_true, Terminal.KEEP)
    ]


### dispatcher

def list_or(a_list):
    return True in a_list


create_metadata_table = """
    CREATE TABLE metadata (
            number_of_species   INTEGER NOT NULL,
            number_of_reactions INTEGER NOT NULL
    );
"""

insert_metadata = """
    INSERT INTO metadata VALUES (?, ?)
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
               decision_tree,
               params={
                   'temperature' : ROOM_TEMP,
                   'electron_free_energy' : -1.4
                   },
               commit_freq=1000,
               number_of_processes=8
               ):
    reaction_queue = Queue()
    table_queue = Queue()
    processes = {}

    bucket_con = sqlite3.connect(bucket_db)
    bucket_cur = bucket_con.cursor()

    res = bucket_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    for name in res:
        table = name[0]
        table_queue.put(table)

    for pid in range(number_of_processes):

        p = Process(
            target=reaction_filter,
            args=(
                mol_entries,
                bucket_db,
                table_queue,
                reaction_queue,
                params,
                decision_tree))

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

    while living_children:
        if reaction_queue.empty():
            living_bools = [processes[pid].is_alive() for pid in processes]
            living_children = list_or(living_bools)

        else:
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

    rn_cur.execute(
        insert_metadata,
        (len(mol_entries) + 1,
         reaction_index + 1))

    rn_con.commit()
    bucket_con.close()
    rn_con.close()


### filter worker

def reaction_filter(mol_entries,
                    bucket_db,
                    table_queue,
                    reaction_queue,
                    params,
                    decision_tree):

    con = sqlite3.connect(bucket_db)
    cur = con.cursor()

    while not table_queue.empty():

        table = table_queue.get()
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
                'number_of_products' : reaction['number_of_reactants']}

            if run_decision_tree(reaction,
                                 mol_entries,
                                 params,
                                 decision_tree):
                reaction_queue.put(reaction)

            if run_decision_tree(reverse_reaction,
                                 mol_entries,
                                 params,
                                 decision_tree):
                reaction_queue.put(reverse_reaction)



