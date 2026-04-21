# backend.py
import pandas as pd
import ast
from typing import Dict, Any, List
import multiprocessing as mp
import time
import copy
import pandas as pd
import random
import math
import copy
import matplotlib.pyplot as plt
import numpy as np
import ast
from typing import Dict, Any, List
from mesa import Agent, Model
from mesa.time import BaseScheduler
import random
import os
from datetime import datetime, timedelta


jours_semaine = ['Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi','Dimanche']

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # ça récupère le dossier du script
DATA_DIR = os.path.join(BASE_DIR, 'data')  
# -----------------------------
# Fonctions contraintes
# -----------------------------
def respect_lit(patient, solution):
    debut = patient['heure_debut_intervention'] + patient['duree_intervention']
    fin = debut + patient['duree_sejour']
    lit = patient['lit']
    for p in solution:
        if p['id'] == patient['id']:
            continue
        if p['lit'] == lit:
            p_debut = p['heure_debut_intervention'] + p['duree_intervention']
            p_fin = p_debut + p['duree_sejour']
            if not (fin <= p_debut or debut >= p_fin):
                return False
    return True

def respect_salle(patient, solution):
    debut = patient['heure_debut_intervention']
    fin = debut + patient['duree_intervention']
    salle = patient['salle']
    for p in solution:
        if p['id'] == patient['id']:
            continue
        if p['salle'] == salle:
            p_debut = p['heure_debut_intervention']
            p_fin = p_debut + p['duree_intervention']
            if not (fin <= p_debut or debut >= p_fin):
                return False
    return True

def respect_chir(patient, solution):
    debut = patient['heure_debut_intervention']
    fin = debut + patient['duree_intervention']
    chir = patient['chirurgien']
    for p in solution:
        if p['id'] == patient['id']:
            continue
        if p['chirurgien'] == chir:
            p_debut = p['heure_debut_intervention']
            p_fin = p_debut + p['duree_intervention']
            if not (fin <= p_debut or debut >= p_fin):
                return False
    return True

def respect_dispo_chir(patient, solution, chirurgiens):
    chir = patient['chirurgien']
    jour = (patient['heure_debut_intervention'] // 24) % 7
    jour_nom = jours_semaine[jour]
    if chir not in chirurgiens:
        return False
    return jour_nom in chirurgiens[chir]['jours_dispo']

# Vérifie qu'un patient ne chevauche pas avec un autre patient pour le même infirmier
def verifier_infirmiers(patient, solution, ibode_dict, iade_dict):
    """
    Renvoie True si on peut assigner les infirmiers nécessaires au patient
    sur le créneau choisi sans dépasser la dispo et sans chevauchement.
    """
    debut = patient['heure_debut_intervention']
    fin = debut + patient['duree_intervention']
    jour = (debut // 24) % 7
    jour_nom = jours_semaine[jour]

    # Vérifie IBODE
    nb_ibode_needed = patient.get('nb_infirmiers_IBODE', 0)
    ibode_dispo = [inf_id for inf_id, inf_data in ibode_dict.items()
                    if jour_nom in inf_data['jours_dispo']
                    and all(fin <= p['heure_debut_intervention'] or debut >= p['heure_debut_intervention'] + p['duree_intervention']
                            for p in solution if inf_id in p.get('infirmiers', []))]
    if len(ibode_dispo) < nb_ibode_needed:
        return False

    # Vérifie IADE
    nb_iade_needed = patient.get('nb_infirmiers_IADE', 0)
    iade_dispo = [inf_id for inf_id, inf_data in iade_dict.items()
                    if jour_nom in inf_data['jours_dispo']
                    and all(fin <= p['heure_debut_intervention'] or debut >= p['heure_debut_intervention'] + p['duree_intervention']
                            for p in solution if inf_id in p.get('infirmiers', []))]
    if len(iade_dispo) < nb_iade_needed:
        return False

    return True



# -----------------------------
# Pour les anesthésistes
# -----------------------------
def respect_anesthesiste(patient, solution):
    debut = patient['heure_debut_intervention']
    fin = debut + patient['duree_intervention']
    anesth = patient['anesthesistes']  # Id ou liste d'anesthésistes
    for p in solution:
        if p['id'] == patient['id']:
            continue
        if p['anesthesistes'] == anesth:
            p_debut = p['heure_debut_intervention']
            p_fin = p_debut + p['duree_intervention']
            if not (fin <= p_debut or debut >= p_fin):
                return False
    return True

def respect_dispo_anesthesiste(patient, solution,anesthesistes):
    """
    Vérifie que l'anesthésiste assigné à `patient` est disponible
    le jour prévu de l'intervention.
    """
    jour = (patient['heure_debut_intervention'] // 24) % 7
    jour_nom = jours_semaine[jour]  # ex: "Lundi", "Mardi", etc.

    anesth = patient.get('anesthesistes', None)
    if anesth is None:
        return False  # aucun anesthésiste assigné

    # Vérification disponibilité dans la solution
    if jour_nom not in anesthesistes[anesth]['jours_dispo']:
        return False

    return True


def cout(solution,anesthesistes,chirurgiens,ibode_dict, iade_dict ):
    c = 0
    
    # 1️⃣ Violations classiques
    for p in solution:
        if not respect_lit(p, solution):
            c += 1
        if not respect_salle(p, solution):
            c += 1
        if not respect_chir(p, solution):
            c += 1
        if not respect_dispo_chir(p, solution,chirurgiens):
            c += 1
        if not verifier_infirmiers(p, solution, ibode_dict, iade_dict):
            c += 1
        if not respect_anesthesiste(p, solution):
            c += 1
        if not respect_dispo_anesthesiste(p, solution, anesthesistes):
            c += 1
        
        # 2️⃣ Vérifier que le séjour commence juste après l'intervention
        debut_sejour_reel = p.get('heure_debut_sejour', p['heure_debut_intervention'] + p['duree_intervention'])
        if debut_sejour_reel != p['heure_debut_intervention'] + p['duree_intervention']:
            c += 1  # coût si le séjour n’est pas directement après l’intervention
    
    # 3️⃣ Lissage occupation lits et salles
    nb_jours = math.ceil(max(p['heure_debut_intervention'] + p['duree_intervention'] + p['duree_sejour'] for p in solution) / 24)
    
    occupation_lits_jour = [0] * nb_jours
    occupation_salles_jour = [0] * nb_jours
    
    for p in solution:
        # Lits
        debut_j = int((p['heure_debut_intervention'] + p['duree_intervention']) // 24)
        fin_j = int(math.ceil((p['heure_debut_intervention'] + p['duree_intervention'] + p['duree_sejour']) / 24))
        for j in range(debut_j, fin_j):
            occupation_lits_jour[j] += 1
        
        # Salles
        debut_s = int(p['heure_debut_intervention'] // 24)
        fin_s = int(math.ceil((p['heure_debut_intervention'] + p['duree_intervention']) / 24))
        for j in range(debut_s, fin_s):
            occupation_salles_jour[j] += 1
    
    # Coût pour variance trop élevée (>2)
    if len(occupation_lits_jour) > 1:
        var_lits = max(occupation_lits_jour)-min(occupation_lits_jour)
        if var_lits > 4:
            c += (var_lits - 4)
    
    if len(occupation_salles_jour) > 1:
        var_salles = max(occupation_salles_jour) - min(occupation_salles_jour)
        if var_salles > 4:
            c += (var_salles - 4)
    
    return c


# -----------------------------
# Fonction pour assignation stricte
# -----------------------------
def peut_assigner(patient, heure, salle, chir, lit,
                occupation_salles, occupation_chir, occupation_lits,
                occupation_anest, occupation_infirmiers,
                duree_interv, duree_sejour):
    debut_interv = heure
    fin_interv = heure + duree_interv
    debut_sejour = fin_interv
    fin_sejour = fin_interv + duree_sejour

    # -----------------------------
    # 🚫 Vérification type de lit selon durée de séjour
    # -----------------------------
    if duree_sejour < 24:  # séjour inférieur à 1 jour
        # Patient ambulatoire : doit aller dans un lit avec "*"
        if "*" not in lit:
            return False
    else:
        # Patient hospitalisé classique : lit sans "*"
        if "*" in lit:
            return False
        
    # -----------------------------
    # Vérification du type de salle selon spécialité
    # ----------------------------
    specialite = patient.get('specialite', None)
    if specialite is None:
        return False

    if specialite in [0, 1]:
        # Spécialité 0 ou 1 → salle "B1..."
        if not salle.startswith("B1"):
            return False
    else:
        # Autres spécialités → pas dans "B1"
        if salle.startswith("B1"):
            return False

    # -----------------------------
    # Vérification salles
    # -----------------------------
    for d, f in occupation_salles[salle]:
        if not (fin_interv <= d or debut_interv >= f):
            return False

    # -----------------------------
    # Vérification chirurgien
    # -----------------------------
    for d, f in occupation_chir[chir]:
        if not (fin_interv <= d or debut_interv >= f):
            return False

    # -----------------------------
    # Vérification lit (sur tout le séjour)
    # -----------------------------
    for d, f in occupation_lits[lit]:
        if not (fin_sejour <= d or debut_sejour >= f):
            return False

    # -----------------------------
    # Vérification anesthésistes (pendant l'intervention)
    # -----------------------------
    anesth = patient.get('anesthesistes', None)
    if anesth is None:
        return False
    for d, f in occupation_anest[anesth]:
        if not (fin_interv <= d or debut_interv >= f):
            return False

    # -----------------------------
    # Si tout est bon
    # -----------------------------
    return True



# -----------------------------
# Génération initiale stricte
# -----------------------------
def solution_initiale_stat(patients, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict,blocs_speciaux , blocs_libres):
    sol = []
    non_assignes = []

    occupation_salles = {s: [] for s in blocs_speciaux + blocs_libres}
    occupation_lits = {l: [] for l in lits}
    occupation_chir = {c: [] for c in chirurgiens}
    occupation_anest = {a: [] for a in anesthesistes}
    occupation_infirmiers = {i: [] for i in list(ibode_dict.keys()) + list(iade_dict.keys())}

    max_hour = 24*30
    list_spe = sorted({d['spe'] for d in chirurgiens.values()})

    for patient in patients:
        print(f" patient {patient['id']} assigné ")
        spe = patient['chirurgien_sp']
        duree_interv = int(patient['duree_intervention'])
        duree_sejour = int(patient['duree_sejour'])
        salles_possibles = blocs_speciaux if spe in list_spe[:2] else blocs_libres
        chir_list = [c for c,d in chirurgiens.items() if int(d['spe'])==int(spe)]
        assigné = False

        random.shuffle(salles_possibles)
        random.shuffle(chir_list)
        anest_list = list(anesthesistes.keys())
        random.shuffle(anest_list)

        # Créer liste de créneaux possibles par jour et heure
        for jour in range(30):
            for h in range(8, 18 - duree_interv + 1):  # plage de 8h à 18h
                heure = jour*24 + h
                for salle in salles_possibles:
                    for chir in chir_list:
                        for lit in lits:
                            for anest in anest_list:
                                nb_ibode = patient.get('nb_infirmiers_IBODE', 0)
                                nb_iade = patient.get('nb_infirmiers_IADE', 0)

                                # Vérifier disponibilité avec les dictionnaires IBODE et IADE
                                ibode_dispo = [i for i, data in ibode_dict.items()
                                                if jours_semaine[jour % 7] in data['jours_dispo']
                                                and all(heure + duree_interv <= occ[0] or heure >= occ[1]
                                                        for occ in occupation_infirmiers[i])]
                                iade_dispo = [i for i, data in iade_dict.items()
                                                if jours_semaine[jour % 7] in data['jours_dispo']
                                                and all(heure + duree_interv <= occ[0] or heure >= occ[1]
                                                        for occ in occupation_infirmiers[i])]

                                if len(ibode_dispo) < nb_ibode or len(iade_dispo) < nb_iade:
                                    continue  # pas assez de personnel disponible

                                selected_infs = ibode_dispo[:nb_ibode] + iade_dispo[:nb_iade]

                                patient_temp = patient.copy()
                                patient_temp.update({
                                    'heure_debut_intervention': heure,
                                    'salle': salle,
                                    'chirurgien': chir,
                                    'lit': lit,
                                    'anesthesistes': anest,
                                    'infirmiers': selected_infs
                                })

                                if peut_assigner(patient_temp, heure, salle, chir, lit,
                                                occupation_salles, occupation_chir, occupation_lits,
                                                occupation_anest, occupation_infirmiers,
                                                duree_interv, duree_sejour):
                                    # ✅ assigner
                                    sol.append(patient_temp)
                                    occupation_salles[salle].append((heure, heure+duree_interv))
                                    occupation_chir[chir].append((heure, heure+duree_interv))
                                    occupation_lits[lit].append((heure+duree_interv, heure+duree_interv+duree_sejour))
                                    occupation_anest[anest].append((heure, heure+duree_interv))
                                    for inf in selected_infs:
                                        occupation_infirmiers[inf].append((heure, heure+duree_interv))
                                    assigné = True
                                    break
                            if assigné: break
                        if assigné: break
                    if assigné: break
                if assigné: break
            if assigné: break

        if not assigné:
            non_assignes.append({
                'id': patient['id'],
                'specialite': spe,
                'raison': 'Pas de créneau disponible'
            })

    return sol, non_assignes




# -----------------------------
# Voisinage intelligent
def voisin_stat(solution, patients, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict, blocs_speciaux, blocs_libres):
    voisin_sol = copy.deepcopy(solution)
    p = random.choice(voisin_sol)
    spe = str(p['specialite'])
    duree_interv = p['duree_intervention']
    duree_sejour = p['duree_sejour']
    max_hour = 24 * 30

    # Construire occupations actuelles
    occupation_salles = {s: [(x['heure_debut_intervention'], x['heure_debut_intervention'] + x['duree_intervention'])
                             for x in voisin_sol if x['salle'] == s] for s in blocs_speciaux + blocs_libres}
    occupation_chir = {c: [(x['heure_debut_intervention'], x['heure_debut_intervention'] + x['duree_intervention'])
                           for x in voisin_sol if x['chirurgien'] == c] for c in chirurgiens}
    occupation_lits = {l: [(x['heure_debut_intervention'] + x['duree_intervention'],
                            x['heure_debut_intervention'] + x['duree_intervention'] + x['duree_sejour'])
                           for x in voisin_sol if x['lit'] == l] for l in lits}
    occupation_anest = {a: [(x['heure_debut_intervention'], x['heure_debut_intervention'] + x['duree_intervention'])
                            for x in voisin_sol if x.get('anesthesistes') == a] for a in anesthesistes}

    occupation_infirmiers = {}
    for i in list(ibode_dict.keys()) + list(iade_dict.keys()):
        occupation_infirmiers[i] = [(x['heure_debut_intervention'], x['heure_debut_intervention'] + x['duree_intervention'])
                                    for x in voisin_sol if i in x.get('infirmiers', [])]

    for _ in range(50):  # essayer jusqu'à 50 fois
        modif = random.choice(['heure', 'salle', 'lit'])

        # 🕒 CHANGEMENT HEURE
        if modif == 'heure':
            jour_idx = random.randint(0, 29)
            max_debut_jour = max(0, int(24 - duree_interv))
            heure_debut = jour_idx * 24 + random.randint(0, max_debut_jour)

            # Vérifier disponibilité IBODE et IADE
            nb_ibode = p.get('nb_infirmiers_IBODE', 0)
            nb_iade = p.get('nb_infirmiers_IADE', 0)

            ibode_dispo = [i for i, data in ibode_dict.items()
                           if jours_semaine[jour_idx % 7] in data['jours_dispo']
                           and all(heure_debut + duree_interv <= occ[0] or heure_debut >= occ[1]
                                   for occ in occupation_infirmiers[i])]
            iade_dispo = [i for i, data in iade_dict.items()
                          if jours_semaine[jour_idx % 7] in data['jours_dispo']
                          and all(heure_debut + duree_interv <= occ[0] or heure_debut >= occ[1]
                                  for occ in occupation_infirmiers[i])]

            if len(ibode_dispo) >= nb_ibode and len(iade_dispo) >= nb_iade:
                selected_infs = ibode_dispo[:nb_ibode] + iade_dispo[:nb_iade]
                p['heure_debut_intervention'] = heure_debut
                p['infirmiers'] = selected_infs
                break

        # 🏥 CHANGEMENT SALLE
        elif modif == 'salle':
            # ✅ Protection contre les listes vides
            if spe in ['0', '1']:
                if blocs_speciaux:
                    nouvelle_salle = random.choice(blocs_speciaux)
                elif blocs_libres:
                    nouvelle_salle = random.choice(blocs_libres)
                else:
                    continue  # aucune salle dispo
            else:
                if blocs_libres:
                    nouvelle_salle = random.choice(blocs_libres)
                elif blocs_speciaux:
                    nouvelle_salle = random.choice(blocs_speciaux)
                else:
                    continue

            if peut_assigner(p, p['heure_debut_intervention'], nouvelle_salle, p['chirurgien'], p['lit'],
                             occupation_salles, occupation_chir, occupation_lits,
                             occupation_anest, occupation_infirmiers,
                             duree_interv, duree_sejour):
                p['salle'] = nouvelle_salle
                break

        # 🛏️ CHANGEMENT LIT
        elif modif == 'lit':
            lits_possibles = [l for l in lits if (('*' in l) if spe in ['0', '1'] else ('*' not in l))]
            if not lits_possibles:
                continue  # aucun lit compatible
            nouveau_lit = random.choice(lits_possibles)
            if peut_assigner(p, p['heure_debut_intervention'], p['salle'], p['chirurgien'], nouveau_lit,
                             occupation_salles, occupation_chir, occupation_lits,
                             occupation_anest, occupation_infirmiers,
                             duree_interv, duree_sejour):
                p['lit'] = nouveau_lit
                break

    return voisin_sol


# -----------------------------
# Recuit simulé avec anesth et infirmiers
# -----------------------------
def recuit_simule_stat(solution_ini, patients, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict, blocs_speciaux,blocs_libres,
                T_init=3000, T_min=0.1, alpha=0.995, max_iter=500):
    historique_cout = []
    current = solution_ini
    print(cout(current,anesthesistes,chirurgiens,ibode_dict, iade_dict))
    best = copy.deepcopy(current)
    T = T_init

    for it in range(max_iter):
        voisin_sol = voisin_stat(current, patients, lits, chirurgiens, anesthesistes,ibode_dict, iade_dict,blocs_speciaux,blocs_libres,)
        delta = cout(voisin_sol,anesthesistes,chirurgiens,ibode_dict, iade_dict) - cout(current,anesthesistes,chirurgiens,ibode_dict, iade_dict)
        if delta < 0 or random.random() < math.exp(-delta / T):
            current = copy.deepcopy(voisin_sol)
            if cout(current,anesthesistes,chirurgiens,ibode_dict, iade_dict) < cout(best,anesthesistes,chirurgiens,ibode_dict, iade_dict):
                best = copy.deepcopy(current)
        T *= alpha
        historique_cout.append(cout(best,anesthesistes,chirurgiens,ibode_dict, iade_dict))
        if T < T_min:
            break

    return best, historique_cout



# -----------------------------
# Génération de voisins multiples
# -----------------------------
def generer_voisins_stat(solution, patients, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict,
                         blocs_speciaux, blocs_libres, N=20):
    voisins = []
    for _ in range(N):
        voisin_temp = copy.deepcopy(solution)
        p = random.choice(voisin_temp)
        spe = str(p['specialite'])
        duree_interv = p['duree_intervention']
        duree_sejour = p['duree_sejour']
        max_hour = 24 * 30

        # Construire occupations actuelles
        occupation_salles = {s: [(x['heure_debut_intervention'],
                                  x['heure_debut_intervention'] + x['duree_intervention'])
                                 for x in voisin_temp if x['salle'] == s] for s in blocs_speciaux + blocs_libres}
        occupation_chir = {c: [(x['heure_debut_intervention'],
                                x['heure_debut_intervention'] + x['duree_intervention'])
                               for x in voisin_temp if x['chirurgien'] == c] for c in chirurgiens}
        occupation_lits = {l: [(x['heure_debut_intervention'] + x['duree_intervention'],
                                x['heure_debut_intervention'] + x['duree_intervention'] + x['duree_sejour'])
                               for x in voisin_temp if x['lit'] == l] for l in lits}
        occupation_anest = {a: [(x['heure_debut_intervention'],
                                 x['heure_debut_intervention'] + x['duree_intervention'])
                                for x in voisin_temp if x.get('anesthesistes') == a] for a in anesthesistes}

        occupation_infirmiers = {}
        for i in list(ibode_dict.keys()) + list(iade_dict.keys()):
            occupation_infirmiers[i] = [(x['heure_debut_intervention'],
                                         x['heure_debut_intervention'] + x['duree_intervention'])
                                        for x in voisin_temp if i in x.get('infirmiers', [])]

        modif = random.choice(['heure', 'salle', 'lit'])

        # 🕒 Modification de l’heure
        if modif == 'heure':
            jour_idx = random.randint(0, 29)
            max_debut_jour = max(0, int(24 - duree_interv))
            heure_debut = jour_idx * 24 + random.randint(0, max_debut_jour)
            nb_ibode = p.get('nb_infirmiers_IBODE', 0)
            nb_iade = p.get('nb_infirmiers_IADE', 0)

            ibode_dispo = [i for i, data in ibode_dict.items()
                           if jours_semaine[jour_idx % 7] in data['jours_dispo']
                           and all(heure_debut + duree_interv <= occ[0] or heure_debut >= occ[1]
                                   for occ in occupation_infirmiers[i])]
            iade_dispo = [i for i, data in iade_dict.items()
                          if jours_semaine[jour_idx % 7] in data['jours_dispo']
                          and all(heure_debut + duree_interv <= occ[0] or heure_debut >= occ[1]
                                  for occ in occupation_infirmiers[i])]

            if len(ibode_dispo) >= nb_ibode and len(iade_dispo) >= nb_iade:
                selected_infs = ibode_dispo[:nb_ibode] + iade_dispo[:nb_iade]
                p['heure_debut_intervention'] = heure_debut
                p['infirmiers'] = selected_infs

        # 🏥 Modification de la salle
        elif modif == 'salle':
            # ✅ Protection contre listes vides
            if spe in ['0', '1']:
                if blocs_speciaux:
                    nouvelle_salle = random.choice(blocs_speciaux)
                elif blocs_libres:
                    nouvelle_salle = random.choice(blocs_libres)
                else:
                    continue  # aucune salle dispo
            else:
                if blocs_libres:
                    nouvelle_salle = random.choice(blocs_libres)
                elif blocs_speciaux:
                    nouvelle_salle = random.choice(blocs_speciaux)
                else:
                    continue

            if peut_assigner(p, p['heure_debut_intervention'], nouvelle_salle, p['chirurgien'], p['lit'],
                             occupation_salles, occupation_chir, occupation_lits,
                             occupation_anest, occupation_infirmiers,
                             duree_interv, duree_sejour):
                p['salle'] = nouvelle_salle

        # 🛏️ Modification du lit
        elif modif == 'lit':
            lits_possibles = [l for l in lits if (('*' in l) if spe in ['0', '1'] else ('*' not in l))]
            if not lits_possibles:
                continue  # aucun lit compatible
            nouveau_lit = random.choice(lits_possibles)
            if peut_assigner(p, p['heure_debut_intervention'], p['salle'], p['chirurgien'], nouveau_lit,
                             occupation_salles, occupation_chir, occupation_lits,
                             occupation_anest, occupation_infirmiers,
                             duree_interv, duree_sejour):
                p['lit'] = nouveau_lit

        # On enregistre le mouvement
        mouvement = (p['id'], modif, p.get(modif if modif != 'heure' else 'heure_debut_intervention'))
        voisins.append((voisin_temp, mouvement))

    return voisins

# -----------------------------
# Algorithme Tabou
# -----------------------------
def tabou_stat(solution_initiale, patients, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict, blocs_speciaux,blocs_libres,
          max_iter=100, taille_tabou=10, N_voisins=50):
    current = solution_initiale
    best = copy.deepcopy(current)
    liste_tabou = []

    historique_cout = [cout(current,anesthesistes,chirurgiens,ibode_dict, iade_dict)]

    for it in range(max_iter):
        voisins = generer_voisins_stat(current, patients, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict,blocs_speciaux,blocs_libres,N=N_voisins)
        
        # Filtrer les mouvements tabous
        candidats = [(v, m) for v,m in voisins if m not in liste_tabou]

        if not candidats:
            candidats = voisins  # si tous tabous, on force à prendre un voisin

        # Choisir le meilleur coût
        voisins_couts = [(v, m, cout(v,anesthesistes,chirurgiens,ibode_dict, iade_dict)) for v, m in candidats]
        voisin_sel, mouvement_sel, cout_voisin = min(voisins_couts, key=lambda x: x[2])

        current = copy.deepcopy(voisin_sel)
        historique_cout.append(cout_voisin)

        # Mettre à jour le meilleur global
        if cout_voisin < cout(best,anesthesistes,chirurgiens,ibode_dict, iade_dict):
            best = copy.deepcopy(current)

        # Mise à jour liste Tabou
        liste_tabou.append(mouvement_sel)
        if len(liste_tabou) > taille_tabou:
            liste_tabou.pop(0)


    return best, historique_cout
# -----------------------------
# Vérification
# -----------------------------
def verifier_violations(solution,anesthesistes,chirurgiens,ibode_dict, iade_dict):
    violation = False
    for patient in solution:
        if not respect_lit(patient, solution):
            print(f"Violation lit pour patient {patient['id']}")
            violation = True
        if not respect_salle(patient, solution):
            print(f"Violation salle pour patient {patient['id']}")
            violation = True
        if not respect_chir(patient, solution):
            print(f"Violation chirurgien pour patient {patient['id']}")
            violation = True
        if not respect_dispo_chir(patient, solution,chirurgiens):
            print(f"Violation dispo chirurgien pour patient {patient['id']}")
            violation = True
        if not respect_anesthesiste(patient, solution):
            print(f"Violation chirurgien pour patient {patient['id']}")
            violation = True
        if not respect_dispo_anesthesiste(patient, solution, anesthesistes):
            print(f"Violation dispo anest pour patient {patient['id']}")
            violation = True
        if not verifier_infirmiers(patient, solution, ibode_dict, iade_dict):
            print(f"Violation inf pour patient {patient['id']}")
            violation = True
    if not violation:
        print("Aucune violation de contrainte dans la solution finale.")
    return violation


class AgentTabouStat:
    def __init__(self, unique_id, model, solution_initiale):
        self.unique_id = unique_id
        self.model = model
        self.solution = solution_initiale
        self.historique = []
        self.cost = float("inf")

    def step(self):
        print("🧠 Agent Tabou : exécution de la recherche tabou...")
        # Partir de la meilleure solution globale actuelle
        if self.model.solution_finale_sma is not None:
            self.solution = copy.deepcopy(self.model.solution_finale_sma)

        # Appel de Tabou
        self.solution, hist = tabou_stat(
            self.solution,
            self.model.patients,
            self.model.lits,
            self.model.chirurgiens,
            self.model.anesthesistes,
            self.model.ibode_dict,
            self.model.iade_dict,
            self.model.blocs_speciaux,
            self.model.blocs_libres
        )

        # Update historique
        self.historique.extend(hist)

        # Calcul du coût
        self.cost = self.model.evaluate_solution(self.solution)

        # Mise à jour de la meilleure solution globale si amélioration
        if self.cost < self.model.best_cost:
            self.model.solution_finale_sma = copy.deepcopy(self.solution)
            self.model.best_cost = self.cost


class AgentRecuitStat:
    def __init__(self, unique_id, model, solution_initiale):
        self.unique_id = unique_id
        self.model = model
        self.solution = solution_initiale
        self.historique = []
        self.cost = float("inf")

    def step(self):
        print("🔥 Agent Recuit : exécution du recuit simulé...")
        # Partir de la meilleure solution globale actuelle
        if self.model.solution_finale_sma is not None:
            self.solution = copy.deepcopy(self.model.solution_finale_sma)

        # Appel du recuit simulé
        self.solution, hist = recuit_simule_stat(
            self.solution,
            self.model.patients,
            self.model.lits,
            self.model.chirurgiens,
            self.model.anesthesistes,
            self.model.ibode_dict,
            self.model.iade_dict,
            self.model.blocs_speciaux,
            self.model.blocs_libres
        )

        # Update historique
        self.historique.extend(hist)

        # Calcul du coût
        self.cost = self.model.evaluate_solution(self.solution)

        # Mise à jour de la meilleure solution globale si amélioration
        if self.cost < self.model.best_cost:
            self.model.solution_finale_sma = copy.deepcopy(self.solution)
            self.model.best_cost = self.cost


class PlanningOptimizationModelStat:
    def __init__(self, patients, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict, blocs_speciaux,blocs_libres, solution_init, n_steps=50):
        self.patients = patients
        self.lits = lits
        self.chirurgiens = chirurgiens
        self.anesthesistes = anesthesistes
        self.ibode_dict = ibode_dict
        self.blocs_speciaux = blocs_speciaux
        self.blocs_libres= blocs_libres
        self.iade_dict = iade_dict
        self.n_steps = n_steps

        self.solution_finale_sma = copy.deepcopy(solution_init)
        self.best_cost = self.evaluate_solution(solution_init)
        self.historique_couts_sma = [self.best_cost]

        # 🚀 Créer les agents
        self.agents = [
            AgentTabouStat(1, self, copy.deepcopy(solution_init)),
            AgentRecuitStat(2, self, copy.deepcopy(solution_init))
        ]

    def evaluate_solution(self, solution):
        if not solution:
            return float("inf")
        return cout(solution, self.anesthesistes, self.chirurgiens, self.ibode_dict, self.iade_dict)

    def step(self):
        # Chaque agent part de la meilleure solution connue
        for agent in self.agents:
            agent.step()
        # Stocker l'historique du meilleur coût après tous les agents
        self.historique_couts_sma.append(self.best_cost)

    def run_model(self):
        for step in range(self.n_steps):
            self.step()
        return self.solution_finale_sma, self.best_cost, self.historique_couts_sma


# -----------------------------
# Génération initiale stricte
# -----------------------------
def solution_initiale_dynamique(patient, solution_existante,lits, chirurgiens, anesthesistes, ibode_dict, iade_dict,blocs_speciaux , blocs_libres):
    sol = copy.deepcopy(solution_existante)  # on conserve le planning existant

    # Initialiser les occupations à partir du planning existant
    occupation_salles = {s: [(x['heure_debut_intervention'], x['heure_debut_intervention']+x['duree_intervention']) 
                             for x in sol if x['salle']==s] 
                         for s in blocs_speciaux + blocs_libres}

    occupation_lits = {l: [(x['heure_debut_intervention']+x['duree_intervention'], x['heure_debut_intervention']+x['duree_intervention']+x['duree_sejour']) 
                           for x in sol if x['lit']==l] 
                       for l in lits}

    occupation_chir = {c: [(x['heure_debut_intervention'], x['heure_debut_intervention']+x['duree_intervention']) 
                           for x in sol if x['chirurgien']==c] 
                       for c in chirurgiens}

    occupation_anest = {a: [(x['heure_debut_intervention'], x['heure_debut_intervention']+x['duree_intervention']) 
                            for x in sol if x.get('anesthesistes')==a] 
                        for a in anesthesistes}

    occupation_infirmiers = {}
    for i in list(ibode_dict.keys()) + list(iade_dict.keys()):
        occupation_infirmiers[i] = [(x['heure_debut_intervention'], x['heure_debut_intervention']+x['duree_intervention'])
                                    for x in sol if i in x.get('infirmiers', [])]
    max_hour = 24*30
    list_spe = sorted({d['spe'] for d in chirurgiens.values()})

    spe = patient['chirurgien_sp']
    duree_interv = int(patient['duree_intervention'])
    duree_sejour = int(patient['duree_sejour'])
    salles_possibles = blocs_speciaux if spe in list_spe[:2] else blocs_libres
    chir_list = [c for c,d in chirurgiens.items() if int(d['spe'])==int(spe)]
    assigné = False

    random.shuffle(salles_possibles)
    random.shuffle(chir_list)
    anest_list = list(anesthesistes.keys())
    random.shuffle(anest_list)
    
    for jour in range(30):
        for h in range(8, 18 - duree_interv + 1):  # plage de 8h à 18h
            heure = jour*24 + h
            for salle in salles_possibles:
                for chir in chir_list:
                    for lit in lits:
                        for anest in anest_list:
                            nb_ibode = patient.get('nb_infirmiers_IBODE', 0)
                            nb_iade = patient.get('nb_infirmiers_IADE', 0)

                            # Vérifier disponibilité avec les dictionnaires IBODE et IADE
                            ibode_dispo = [i for i, data in ibode_dict.items()
                                            if jours_semaine[jour % 7] in data['jours_dispo']
                                            and all(heure + duree_interv <= occ[0] or heure >= occ[1]
                                                    for occ in occupation_infirmiers[i])]
                            iade_dispo = [i for i, data in iade_dict.items()
                                            if jours_semaine[jour % 7] in data['jours_dispo']
                                            and all(heure + duree_interv <= occ[0] or heure >= occ[1]
                                                    for occ in occupation_infirmiers[i])]

                            if len(ibode_dispo) < nb_ibode or len(iade_dispo) < nb_iade:
                                continue  # pas assez de personnel disponible

                            selected_infs = ibode_dispo[:nb_ibode] + iade_dispo[:nb_iade]

                            patient_temp = patient.copy()
                            patient_temp.update({
                                'heure_debut_intervention': heure,
                                'salle': salle,
                                'chirurgien': chir,
                                'lit': lit,
                                'anesthesistes': anest,
                                'infirmiers': selected_infs
                            })

                            if peut_assigner(patient_temp, heure, salle, chir, lit,
                                            occupation_salles, occupation_chir, occupation_lits,
                                            occupation_anest, occupation_infirmiers,
                                            duree_interv, duree_sejour):
                                # ✅ assigner
                                sol.append(patient_temp)
                                occupation_salles[salle].append((heure, heure+duree_interv))
                                occupation_chir[chir].append((heure, heure+duree_interv))
                                occupation_lits[lit].append((heure+duree_interv, heure+duree_interv+duree_sejour))
                                occupation_anest[anest].append((heure, heure+duree_interv))
                                for inf in selected_infs:
                                    occupation_infirmiers[inf].append((heure, heure+duree_interv))
                                assigné = True
                                break
                        if assigné: break
                    if assigné: break
                if assigné: break
            if assigné: break
        if assigné: break

        if not assigné:
            print (patient['id'] ,'non assigné ')

    return sol


def voisin_dynamique(solution_existante, patient, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict, blocs_speciaux, blocs_libres):
    """
    Retourne une nouvelle solution où on perturbe uniquement 
    le planning d'un patient (présent dans la solution),
    sans modifier directement le dict 'patient' passé en paramètre.
    """
    voisin_sol = copy.deepcopy(solution_existante)

    # Trouver le patient dans la solution courante
    patient_idx = next((i for i, p in enumerate(voisin_sol) if p['id'] == patient['id']), None)
    if patient_idx is None:
        print(f"⚠️ Patient {patient['id']} introuvable dans la solution.")
        return voisin_sol

    p_voisin = copy.deepcopy(voisin_sol[patient_idx])

    # Construire les occupations à partir du planning actuel
    occupation_salles = {s: [(x['heure_debut_intervention'], x['heure_debut_intervention'] + x['duree_intervention'])
                             for x in voisin_sol if x['salle'] == s and x['id'] != p_voisin['id']]
                         for s in blocs_speciaux + blocs_libres}

    occupation_chir = {c: [(x['heure_debut_intervention'], x['heure_debut_intervention'] + x['duree_intervention'])
                           for x in voisin_sol if x['chirurgien'] == c and x['id'] != p_voisin['id']]
                       for c in chirurgiens}

    occupation_lits = {l: [(x['heure_debut_intervention'] + x['duree_intervention'],
                            x['heure_debut_intervention'] + x['duree_intervention'] + x['duree_sejour'])
                           for x in voisin_sol if x['lit'] == l and x['id'] != p_voisin['id']]
                       for l in lits}

    occupation_anest = {a: [(x['heure_debut_intervention'], x['heure_debut_intervention'] + x['duree_intervention'])
                            for x in voisin_sol if x.get('anesthesistes') == a and x['id'] != p_voisin['id']]
                        for a in anesthesistes}

    occupation_infirmiers = {
        i: [(x['heure_debut_intervention'], x['heure_debut_intervention'] + x['duree_intervention'])
            for x in voisin_sol if i in x.get('infirmiers', []) and x['id'] != p_voisin['id']]
        for i in list(ibode_dict.keys()) + list(iade_dict.keys())
    }

    # Perturbation
    spe = str(p_voisin['specialite'])
    duree_interv = p_voisin['duree_intervention']
    duree_sejour = p_voisin['duree_sejour']

    for _ in range(50):  # essais aléatoires
        modif = random.choice(['heure', 'salle', 'lit'])

        if modif == 'heure':
            jour_idx = random.randint(0, 29)
            max_debut_jour = max(0, int(24 - duree_interv))
            nouvelle_heure = jour_idx * 24 + random.randint(0, max_debut_jour)

            nb_ibode = p_voisin.get('nb_infirmiers_IBODE', 0)
            nb_iade = p_voisin.get('nb_infirmiers_IADE', 0)

            ibode_dispo = [i for i, data in ibode_dict.items()
                           if jours_semaine[jour_idx % 7] in data['jours_dispo']
                           and all(nouvelle_heure + duree_interv <= occ[0] or nouvelle_heure >= occ[1]
                                   for occ in occupation_infirmiers[i])]
            iade_dispo = [i for i, data in iade_dict.items()
                          if jours_semaine[jour_idx % 7] in data['jours_dispo']
                          and all(nouvelle_heure + duree_interv <= occ[0] or nouvelle_heure >= occ[1]
                                  for occ in occupation_infirmiers[i])]

            if len(ibode_dispo) >= nb_ibode and len(iade_dispo) >= nb_iade:
                p_voisin['heure_debut_intervention'] = nouvelle_heure
                p_voisin['infirmiers'] = ibode_dispo[:nb_ibode] + iade_dispo[:nb_iade]
                break

        elif modif == 'salle':
            nouvelle_salle = random.choice(blocs_speciaux if spe in ['0', '1'] else blocs_libres)
            if peut_assigner(p_voisin, p_voisin['heure_debut_intervention'], nouvelle_salle, p_voisin['chirurgien'],
                            p_voisin['lit'], occupation_salles, occupation_chir, occupation_lits,
                            occupation_anest, occupation_infirmiers, duree_interv, duree_sejour):
                p_voisin['salle'] = nouvelle_salle
                break

        elif modif == 'lit':
            nouveau_lit = random.choice([l for l in lits if (('*' in l) if spe in ['0', '1'] else ('*' not in l))])
            if peut_assigner(p_voisin, p_voisin['heure_debut_intervention'], p_voisin['salle'], p_voisin['chirurgien'],
                            nouveau_lit, occupation_salles, occupation_chir, occupation_lits, occupation_anest,
                            occupation_infirmiers, duree_interv, duree_sejour):
                p_voisin['lit'] = nouveau_lit
                break

    # Réinjecter le patient perturbé dans la solution voisine
    voisin_sol[patient_idx] = p_voisin
    return voisin_sol


# -----------------------------
# Recuit simulé avec anesth et infirmiers
# -----------------------------
def recuit_simule_dynamique(solution_init ,patient, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict,blocs_speciaux,blocs_libres, 
                T_init=300, T_min=0.1, alpha=0.995, max_iter=500):

    historique_cout = []
    current = solution_init
    print(cout(solution_init,anesthesistes,chirurgiens,ibode_dict, iade_dict))
    best = copy.deepcopy(current)
    T = T_init

    for it in range(max_iter):
        voisin_sol = voisin_dynamique(current, patient, lits, chirurgiens, anesthesistes,ibode_dict, iade_dict, blocs_speciaux, blocs_libres)
        delta = cout(voisin_sol,anesthesistes,chirurgiens,ibode_dict, iade_dict) - cout(current,anesthesistes,chirurgiens,ibode_dict, iade_dict)
        if delta < 0 or random.random() < math.exp(-delta / T):
            current = copy.deepcopy(voisin_sol)
            if cout(current,anesthesistes,chirurgiens,ibode_dict, iade_dict) < cout(best,anesthesistes,chirurgiens,ibode_dict, iade_dict):
                best = copy.deepcopy(current)
        T *= alpha
        historique_cout.append(cout(best,anesthesistes,chirurgiens,ibode_dict, iade_dict))
        if T < T_min:
            break

    return best, historique_cout


# -----------------------------
# Génération de voisins — version dynamique (patient ajouté uniquement)
# -----------------------------
def generer_voisins_dynamique(solution, patient, lits, chirurgiens, anesthesistes,
                              ibode_dict, iade_dict, blocs_speciaux, blocs_libres, N=10):
    """
    Génère N solutions voisines en modifiant uniquement le patient ajouté.
    Chaque voisin correspond à une petite perturbation (heure, salle, ou lit).
    """
    voisins = []
    patient_id = patient['id']

    # Trouver le patient ajouté dans la solution courante
    patient_sol = next((p for p in solution if p['id'] == patient_id), None)
    if patient_sol is None:
        print(f"⚠️ Patient {patient_id} introuvable dans la solution.")
        return []

    for _ in range(N):
        voisin_temp = copy.deepcopy(solution)
        p_voisin = next(p for p in voisin_temp if p['id'] == patient_id)

        spe = str(p_voisin['specialite'])
        duree_interv = p_voisin['duree_intervention']
        duree_sejour = p_voisin['duree_sejour']

        # Recalcul des occupations sans ce patient
        occupation_salles = {s: [(x['heure_debut_intervention'], x['heure_debut_intervention']+x['duree_intervention'])
                                 for x in voisin_temp if x['salle'] == s and x['id'] != patient_id]
                             for s in blocs_speciaux + blocs_libres}
        occupation_chir = {c: [(x['heure_debut_intervention'], x['heure_debut_intervention']+x['duree_intervention'])
                               for x in voisin_temp if x['chirurgien'] == c and x['id'] != patient_id]
                           for c in chirurgiens}
        occupation_lits = {l: [(x['heure_debut_intervention']+x['duree_intervention'],
                                x['heure_debut_intervention']+x['duree_intervention']+x['duree_sejour'])
                               for x in voisin_temp if x['lit'] == l and x['id'] != patient_id]
                           for l in lits}
        occupation_anest = {a: [(x['heure_debut_intervention'], x['heure_debut_intervention']+x['duree_intervention'])
                                for x in voisin_temp if x.get('anesthesistes') == a and x['id'] != patient_id]
                            for a in anesthesistes}

        occupation_infirmiers = {
            i: [(x['heure_debut_intervention'], x['heure_debut_intervention']+x['duree_intervention'])
                for x in voisin_temp if i in x.get('infirmiers', []) and x['id'] != patient_id]
            for i in list(ibode_dict.keys()) + list(iade_dict.keys())
        }

        modif = random.choice(['heure', 'salle', 'lit'])

        if modif == 'heure':
            jour_idx = random.randint(0, 29)
            max_debut_jour = max(0, int(24 - duree_interv))
            heure_debut = jour_idx * 24 + random.randint(8, 18 - duree_interv)
            
            # Vérifier dispo du personnel
            nb_ibode = p_voisin.get('nb_infirmiers_IBODE', 0)
            nb_iade = p_voisin.get('nb_infirmiers_IADE', 0)
            ibode_dispo = [i for i, data in ibode_dict.items()
                           if jours_semaine[jour_idx % 7] in data['jours_dispo']
                           and all(heure_debut + duree_interv <= occ[0] or heure_debut >= occ[1]
                                   for occ in occupation_infirmiers[i])]
            iade_dispo = [i for i, data in iade_dict.items()
                          if jours_semaine[jour_idx % 7] in data['jours_dispo']
                          and all(heure_debut + duree_interv <= occ[0] or heure_debut >= occ[1]
                                  for occ in occupation_infirmiers[i])]

            if len(ibode_dispo) >= nb_ibode and len(iade_dispo) >= nb_iade:
                p_voisin['heure_debut_intervention'] = heure_debut
                p_voisin['infirmiers'] = ibode_dispo[:nb_ibode] + iade_dispo[:nb_iade]

        elif modif == 'salle':
            nouvelle_salle = random.choice(blocs_speciaux if spe in ['0','1'] else blocs_libres)
            if peut_assigner(p_voisin, p_voisin['heure_debut_intervention'], nouvelle_salle, p_voisin['chirurgien'],
                             p_voisin['lit'], occupation_salles, occupation_chir, occupation_lits,
                             occupation_anest, occupation_infirmiers, duree_interv, duree_sejour):
                p_voisin['salle'] = nouvelle_salle

        elif modif == 'lit':
            nouveau_lit = random.choice([l for l in lits if (('*' in l) if spe in ['0','1'] else ('*' not in l))])
            if peut_assigner(p_voisin, p_voisin['heure_debut_intervention'], p_voisin['salle'], p_voisin['chirurgien'],
                             nouveau_lit, occupation_salles, occupation_chir, occupation_lits,
                             occupation_anest, occupation_infirmiers, duree_interv, duree_sejour):
                p_voisin['lit'] = nouveau_lit

        mouvement = (patient_id, modif, p_voisin.get(modif if modif != 'heure' else 'heure_debut_intervention'))
        voisins.append((voisin_temp, mouvement))

    return voisins


# -----------------------------
# Algorithme Tabou — version dynamique (ajout patient)
# -----------------------------
def tabou_dynamique(solution_init, patient, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict,blocs_speciaux, blocs_libres, max_iter=200, taille_tabou=50, N_voisins=20):
    """
    Recherche tabou après ajout d’un patient.
    - Le patient est ajouté via solution_initiale()
    - Puis la recherche tabou optimise uniquement sa position
    """


    current = copy.deepcopy(solution_init)
    best = copy.deepcopy(current)
    liste_tabou = []
    historique_cout = [cout(current,anesthesistes,chirurgiens,ibode_dict, iade_dict)]

    print(f"✅ Patient {patient['id']} ajouté — coût initial : {cout(current,anesthesistes,chirurgiens,ibode_dict, iade_dict)}")

    # Étape 2 : optimisation tabou
    for it in range(max_iter):
        voisins = generer_voisins_dynamique(current, patient, lits, chirurgiens, anesthesistes,
                                            ibode_dict, iade_dict, blocs_speciaux, blocs_libres, N=N_voisins)

        candidats = [(v, m) for v, m in voisins if m not in liste_tabou]
        if not candidats:
            candidats = voisins

        if not candidats:
            break  # aucun voisin valide

        voisins_couts = [(v, m, cout(v,anesthesistes,chirurgiens,ibode_dict, iade_dict)) for v, m in candidats]
        voisin_sel, mouvement_sel, cout_voisin = min(voisins_couts, key=lambda x: x[2])

        current = copy.deepcopy(voisin_sel)
        historique_cout.append(cout_voisin)

        if cout_voisin < cout(best,anesthesistes,chirurgiens,ibode_dict, iade_dict):
            best = copy.deepcopy(current)

        # mise à jour liste tabou
        liste_tabou.append(mouvement_sel)
        if len(liste_tabou) > taille_tabou:
            liste_tabou.pop(0)

        if it % 100 == 0 or it == max_iter - 1:
            print(f"Iteration {it}, coût actuel : {cout_voisin}, meilleur : {cout(best,anesthesistes,chirurgiens,ibode_dict, iade_dict)}")

    print(f"🏁 Tabou terminé — coût final : {cout(best,anesthesistes,chirurgiens,ibode_dict, iade_dict)}")
    return best, historique_cout


class AgentTabouDynamique:
    def __init__(self, unique_id, model, solution_initiale):
        self.unique_id = unique_id
        self.model = model
        self.solution = solution_initiale
        self.historique = []
        self.cost = float("inf")

    def step(self):
        print("🧠 Agent Tabou : exécution de la recherche tabou...")
        # Partir de la meilleure solution globale actuelle
        if self.model.solution_finale_sma is not None:
            self.solution = copy.deepcopy(self.model.solution_finale_sma)

        # Appel de Tabou
        
        self.solution, hist = tabou_dynamique(
            self.solution,
            self.model.patient,
            self.model.lits,
            self.model.chirurgiens,
            self.model.anesthesistes,
            self.model.ibode_dict,
            self.model.iade_dict,
            self.model.blocs_speciaux,
            self.model.blocs_libres, 
        )

        # Update historique
        self.historique.extend(hist)

        # Calcul du coût
        self.cost = self.model.evaluate_solution(self.solution)

        # Mise à jour de la meilleure solution globale si amélioration
        if self.cost < self.model.best_cost:
            self.model.solution_finale_sma = copy.deepcopy(self.solution)
            self.model.best_cost = self.cost


class AgentRecuitDynamiques:
    def __init__(self, unique_id, model, solution_initiale):
        self.unique_id = unique_id
        self.model = model
        self.solution = solution_initiale
        self.historique = []
        self.cost = float("inf")

    def step(self):
        print("🔥 Agent Recuit : exécution du recuit simulé...")
        # Partir de la meilleure solution globale actuelle
        if self.model.solution_finale_sma is not None:
            self.solution = copy.deepcopy(self.model.solution_finale_sma)

        # Appel du recuit simulé
        self.solution, hist = recuit_simule_dynamique(
            self.solution,
            self.model.patient,
            self.model.lits,
            self.model.chirurgiens,
            self.model.anesthesistes,
            self.model.ibode_dict,
            self.model.iade_dict,
            self.model.blocs_speciaux,
            self.model.blocs_libres, 
        )

        # Update historique
        self.historique.extend(hist)

        # Calcul du coût
        self.cost = self.model.evaluate_solution(self.solution)

        # Mise à jour de la meilleure solution globale si amélioration
        if self.cost < self.model.best_cost:
            self.model.solution_finale_sma = copy.deepcopy(self.solution)
            self.model.best_cost = self.cost


class PlanningOptimizationModelDynamique:
    def __init__(self, patient, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict, solution_init, blocs_speciaux, blocs_libres, n_steps=50):
        self.patient = patient
        self.lits = lits
        self.chirurgiens = chirurgiens
        self.anesthesistes = anesthesistes
        self.ibode_dict = ibode_dict
        self.iade_dict = iade_dict
        self.blocs_speciaux = blocs_speciaux
        self.blocs_libres = blocs_libres
        self.n_steps = n_steps

        self.solution_finale_sma = copy.deepcopy(solution_init)
        self.best_cost = self.evaluate_solution(solution_init)
        self.historique_couts_sma = [self.best_cost]

        # 🚀 Créer les agents
        self.agents = [
            AgentTabouDynamique(1, self, copy.deepcopy(solution_init)),
            AgentRecuitDynamiques(2, self, copy.deepcopy(solution_init))
        ]

    def evaluate_solution(self, solution):
        if not solution:
            return float("inf")
        return cout(solution,self.anesthesistes,self.chirurgiens,self.ibode_dict, self.iade_dict)

    def step(self):
        # Chaque agent part de la meilleure solution connue
        for agent in self.agents:
            agent.step()
        # Stocker l'historique du meilleur coût après tous les agents
        self.historique_couts_sma.append(self.best_cost)

    def run_model(self):
        for step in range(self.n_steps):
            print(f"\n===== ITERATION {step+1}/{self.n_steps} =====")
            self.step()
        print("\n🏁 Optimisation terminée.")
        print(f"✅ Meilleur coût : {self.best_cost}")
        return self.solution_finale_sma, self.best_cost, self.historique_couts_sma
    
    def plot_convergence_curves(self):
        plt.figure()
        plt.plot(self.agents[0].historique, label="Tabu Search Agent")
        plt.plot(self.agents[1].historique, label="RS Agent")
        plt.legend()
        plt.xlabel("Iteration number")
        plt.ylabel("Value of cost function")
        plt.title("Convergence curves of Tabu Search and RS agents in SMA")
        plt.show()


# -----------------------
# Génération d’un candidat pour un patient ajouté
# -----------------------
def generate_candidate_genetique(nouveau_patient, planning_existant,
                                 chirurgiens, ibode_dict, iade_dict,
                                 anesthesistes, lits, blocs_speciaux, blocs_libres):
    """
    Génère une insertion réaliste (candidate) du nouveau patient
    dans le planning existant en respectant les contraintes.
    """
    spe = int(nouveau_patient.get('specialite', 0))
    duree_interv = int(nouveau_patient.get('duree_intervention', random.randint(1, 4)))
    duree_sejour = int(nouveau_patient.get('duree_sejour', random.randint(1, 5)))
    chir_list = [c for c, d in chirurgiens.items() if int(d['spe']) == spe] or list(chirurgiens.keys())

    salles_possibles = blocs_speciaux if spe in [0, 1] else blocs_libres
    if not salles_possibles:
        salles_possibles = blocs_libres + blocs_speciaux

    for _ in range(400):  # essais aléatoires
        jour = random.randint(0, 29)
        heure = jour * 24 + random.randint(8, 16)
        salle = random.choice(salles_possibles)
        chir = random.choice(chir_list)
        lit = random.choice(lits)

        anesth = random.choice(list(anesthesistes.keys()))
        nb_ibode = nouveau_patient.get('nb_infirmiers_IBODE', 0)
        nb_iade = nouveau_patient.get('nb_infirmiers_IADE', 0)

        ibode_dispo = [i for i, d in ibode_dict.items() if jours_semaine[jour % 7] in d['jours_dispo']]
        iade_dispo = [i for i, d in iade_dict.items() if jours_semaine[jour % 7] in d['jours_dispo']]

        if len(ibode_dispo) < nb_ibode or len(iade_dispo) < nb_iade:
            continue

        infirmiers = ibode_dispo[:nb_ibode] + iade_dispo[:nb_iade]

        candidate = copy.deepcopy(nouveau_patient)
        candidate.update({
            'heure_debut_intervention': heure,
            'salle': salle,
            'chirurgien': chir,
            'lit': lit,
            'anesthesistes': anesth,
            'infirmiers': infirmiers
        })

        if (respect_lit(candidate, planning_existant) and
            respect_salle(candidate, planning_existant) and
            respect_chir(candidate, planning_existant) and
            respect_dispo_chir(candidate, planning_existant, chirurgiens) and
            verifier_infirmiers(candidate, planning_existant, ibode_dict, iade_dict) and
            respect_anesthesiste(candidate, planning_existant) and
            respect_dispo_anesthesiste(candidate, planning_existant, anesthesistes)):
            return candidate
    return None


# -----------------------
# Initialisation de la population
# -----------------------
def init_population_genetique(nouveau_patient, planning_existant,
                              chirurgiens, ibode_dict, iade_dict,
                              anesthesistes, lits, blocs_speciaux, blocs_libres,
                              pop_size=10):
    population = []
    for _ in range(pop_size):
        cand = generate_candidate_genetique(nouveau_patient, planning_existant,
                                            chirurgiens, ibode_dict, iade_dict,
                                            anesthesistes, lits, blocs_speciaux, blocs_libres)
        if cand:
            sol = planning_existant + [cand]
            c = cout(sol, anesthesistes, chirurgiens, ibode_dict, iade_dict)
            population.append({'solution': sol, 'cout': c})
    return population


# -----------------------
# Sélection, croisement, mutation
# -----------------------
def tournament_selection(pop, k=3):
    sample = random.sample(pop, min(k, len(pop)))
    return min(sample, key=lambda x: x['cout'])

def crossover(p1, p2, patient_id):
    s1 = [p for p in p1['solution'] if p['id'] != patient_id]
    s2 = [p for p in p2['solution'] if p['id'] == patient_id]
    child = copy.deepcopy(p1)
    if s2:
        child['solution'] = s1 + [copy.deepcopy(random.choice(s2))]
    return child

def mutation(ind, nouveau_patient, planning_existant,
             chirurgiens, ibode_dict, iade_dict,
             anesthesistes, lits, blocs_speciaux, blocs_libres, rate=0.4):
    sol = [p for p in ind['solution'] if p['id'] != nouveau_patient['id']]
    if random.random() < rate:
        cand = generate_candidate_genetique(nouveau_patient, sol, chirurgiens, ibode_dict, iade_dict,
                                            anesthesistes, lits, blocs_speciaux, blocs_libres)
        if cand:
            return {'solution': sol + [cand], 'cout': None}
    return ind


# -----------------------
# Algorithme génétique principal
# -----------------------
def algorithme_genetique_insertion(nouveau_patient, planning_existant,
                                   chirurgiens, ibode_dict, iade_dict,
                                   anesthesistes, lits, blocs_speciaux, blocs_libres,
                                   pop_size=10, generations=15):
    population = init_population_genetique(nouveau_patient, planning_existant,
                                           chirurgiens, ibode_dict, iade_dict,
                                           anesthesistes, lits, blocs_speciaux, blocs_libres,
                                           pop_size)

    for gen in range(generations):
        population.sort(key=lambda x: x['cout'])
        best = population[0]

        if best['cout'] == 0:
            break

        new_pop = [copy.deepcopy(best)]
        while len(new_pop) < pop_size:
            p1 = tournament_selection(population)
            p2 = tournament_selection(population)
            child = crossover(p1, p2, nouveau_patient['id'])
            child = mutation(child, nouveau_patient, planning_existant,
                             chirurgiens, ibode_dict, iade_dict,
                             anesthesistes, lits, blocs_speciaux, blocs_libres)
            if child['cout'] is None:
                child['cout'] = cout(child['solution'], anesthesistes, chirurgiens, ibode_dict, iade_dict)
            new_pop.append(child)
        population = new_pop

    population.sort(key=lambda x: x['cout'])
    return population[0]['solution']

# ---------------------------
# Module 1 : prédiction DMS
# ---------------------------
def load_models_and_preprocessors():
    import pandas as pd
    import numpy as np
    from sklearn.preprocessing import OneHotEncoder
    from sklearn.compose import ColumnTransformer
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import Pipeline
    from xgboost import XGBRegressor
    from sklearn.ensemble import RandomForestRegressor, StackingRegressor


    # --- Charger et merger les données ---
    code_en_mot = pd.read_csv(os.path.join(DATA_DIR, 'ccam_en_mot.csv'), sep=';', encoding='utf-8')
    df = pd.read_csv(os.path.join(DATA_DIR, 'AH_chir.csv'), sep=';', encoding='utf-8')
    code_en_mot ['acte_classant'] = code_en_mot['Code']
    df = df.merge(code_en_mot, on="acte_classant", how="left")
    ccam_spe = pd.read_csv(os.path.join(DATA_DIR, 'ccam_spes.csv'), sep=';', encoding='utf-8')
    code_en_mot ['acte_classant'] = code_en_mot['Code'] 
    ccam_spe_dict = dict(zip(ccam_spe['acte_classant'], ccam_spe['specialite']))
    df['spe'] = df['acte_classant'].map(ccam_spe_dict)

    df['acte_type_simple'] = df['Spécialité'].apply(
        lambda x: (
            "Thérapeutique" if "THÉRAPEUTIQUES" in x else
            "Diagnostique" if "DIAGNOSTIQUES" in x else
            "Gestes complémentaires" if "GESTES COMPLÉMENTAIRES" in x else
            "Forfaits / actes transitoires" if "FORFAITS ET ACTES TRANSITOIRES" in x else
            "Suppléments" if "SUPPLÉMENTS" in x else
            "Radiothérapie externe" if "RADIOTHÉRAPIE EXTERNE" in x else
            "Autre"
        )
    )

    # --- Dates et features temporelles ---
    # faire 2 df différent (duree en heures, duree en jours)
    df["date_entree"] = pd.to_datetime(df["date_entree"])
    df["date_sortie"] = pd.to_datetime(df["date_sortie"])

    df_diff_day = df[df["date_entree"] != df["date_sortie"]].copy()

    df_diff_day['duree_jours_init'] = (df_diff_day['date_sortie'] - df_diff_day['date_entree']).dt.days
    df_diff_day['jour_semaine_entree'] = df_diff_day['date_entree'].dt.weekday
    df_diff_day['mois_entree'] = df_diff_day['date_entree'].dt.month

    df_same_day = df[df["date_entree"] == df["date_sortie"]].copy()

    # --- Features temporelles ---
    df_same_day['jour_semaine_entree'] = df_same_day['date_entree'].dt.weekday
    df_same_day['mois_entree'] = df_same_day['date_entree'].dt.month


    # --- Chargement de la base externe ---
    ccam_pas_dans_la_bdd = pd.read_csv(
        os.path.join(DATA_DIR, 'ccam_pas_dans_la_base.csv'),
        sep=';', encoding='utf-8'
    )
    ccam_pas_dans_la_bdd['acte'] = ccam_pas_dans_la_bdd['acte'].astype(str).str[:7]
    ccam_pas_dans_la_bdd['dms_globale'] = (
        ccam_pas_dans_la_bdd['dms_globale']
        .astype(str)
        .str.replace(',', '.')
        .astype(float)
    )


    # --- Conversion de colonnes en catégories ---
    categorical_cols = ['sexe', 'type_ghm']
    for col in categorical_cols:
        if col in df.columns:
            df[col] = df[col].astype('category')

    ## -------- Modèle de prédiction pour les séjours longs -----------

    # --- Création du préprocesseur pour les séjours longs ---
    cat_cols_diff = ['sexe', 'dp', 'acte_type_simple', 'mode_entree', 'type_mode']
    preprocessor_diff = ColumnTransformer(
        transformers=[
            ("cat", OneHotEncoder(handle_unknown="ignore"), cat_cols_diff)
        ],
        remainder="passthrough"
    )


    # --- Encodage des données pour le stacking ---
    X_diff = df_diff_day[['age', 'sexe', 'dp', 'acte_classant', 'acte_type_simple', 'mode_entree',
                        'duree_jours_init', 'jour_semaine_entree', 'mois_entree']].copy()
    y_diff = df_diff_day['duree_totale'].apply(np.log1p)

    # Frequency encoding
    freq = X_diff['acte_classant'].value_counts() / len(X_diff)
    X_diff['acte_classant_enc'] = X_diff['acte_classant'].map(freq)
    X_diff.drop('acte_classant', axis=1, inplace=True)

    # Feature croisée
    X_diff['type_mode'] = X_diff['acte_type_simple'].astype(str) + "_" + X_diff['mode_entree'].astype(str)

    # Encodage final
    X_encoded_diff = preprocessor_diff.fit_transform(X_diff)

    # --- Split et entraînement du modèle de stacking ---
    X_train_diff, X_test_diff, y_train_diff, y_test_diff = train_test_split(X_encoded_diff, y_diff, test_size=0.2, random_state=42)

    xgb_model = XGBRegressor(objective="reg:squarederror", n_estimators=300, max_depth=8,
                learning_rate=0.05, subsample=0.8, colsample_bytree=0.8, random_state=42)

    rfr_model = RandomForestRegressor(n_estimators=500, max_depth=15, random_state=42)

    stack_long = StackingRegressor(
        estimators=[('xgb', xgb_model), ('rfr', rfr_model)],
        final_estimator=RandomForestRegressor(n_estimators=300, random_state=42),
        cv=3,
        n_jobs=-1,
        passthrough=True
    )

    stack_long.fit(X_train_diff, y_train_diff)



    ## -------- Modèle de prédiction pour les séjours courts -----------

    X_same = df_same_day[['age', 'sexe', 'dp', 'acte_classant', 'acte_type_simple', 'mode_entree',
            'jour_semaine_entree', 'mois_entree']].copy()
    y_same = df_same_day['duree_totale']

    # --- Target encoding pour acte_classant ---
    acte_mean = df_same_day.groupby('acte_classant')['duree_totale'].mean()
    X_same['acte_classant_te'] = X_same['acte_classant'].map(acte_mean)
    X_same.drop('acte_classant', axis=1, inplace=True)

    # --- Features croisées ---
    X_same['type_mode'] = X_same['acte_type_simple'].astype(str) + "_" + X_same['mode_entree'].astype(str)
    # --- Split train/test ---
    X_train_same, X_test_same, y_train_same, y_test_same = train_test_split(X_same, y_same, test_size=0.2, random_state=42)

    # --- Préprocesseur pour les séjours le même jour ---
    cat_cols_same = ['sexe', 'dp', 'acte_type_simple', 'mode_entree', 'type_mode']
    preprocessor_short = ColumnTransformer(
        transformers=[
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), cat_cols_same)
        ],
        remainder="passthrough"
    )

    # --- Pipeline XGBoost ---
    pipeline_short = Pipeline(steps=[
        ("preprocessor", preprocessor_short),
        ("regressor", XGBRegressor(
            objective="reg:squarederror",
            n_estimators=300,
            max_depth=6,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42
        ))
    ])

    pipeline_short.fit(X_train_same, y_train_same)
    y_hat_same = pipeline_short.predict(X_test_same)

    return pipeline_short, stack_long, preprocessor_short, preprocessor_diff

def predict_dms(patient_row, pipeline_short, stack_long, preprocessor_short, preprocessor_diff, df_same_day, df_diff_day, dg):
    import numpy as np
    import pandas as pd
    
    ccam_code = patient_row['ccam']
    
    # Séjours courts
    row_same = df_same_day[df_same_day["acte_classant"] == ccam_code]
    if not row_same.empty:
        dms_row = row_same.iloc[0:1].copy()
        dms_row['type_mode'] = dms_row['acte_type_simple'].astype(str) + "_" + dms_row['mode_entree'].astype(str)
        te = df_same_day.groupby('acte_classant')['duree_totale'].mean()
        dms_row['acte_classant_te'] = dms_row['acte_classant'].map(te)
        for col in ['jour_semaine_entree', 'mois_entree']:
            if col not in dms_row.columns:
                dms_row[col] = pd.to_datetime(dms_row['date_entree']).dt.__getattribute__(col.split('_')[0]).iloc[0]
        missing_cols = set(preprocessor_short.feature_names_in_) - set(dms_row.columns)
        for col in missing_cols:
            dms_row[col] = np.nan
        X_pred = preprocessor_short.transform(dms_row)
        pred = pipeline_short.named_steps["regressor"].predict(X_pred)
        return round(float(pred[0]), 1), "heures"

    # Séjours longs
    row_diff = df_diff_day[df_diff_day["acte_classant"] == ccam_code]
    if not row_diff.empty:
        dms_row = row_diff.iloc[0:1].copy()
        dms_row['type_mode'] = dms_row['acte_type_simple'].astype(str) + "_" + dms_row['mode_entree'].astype(str)
        freq = df_diff_day['acte_classant'].value_counts() / len(df_diff_day)
        dms_row['acte_classant_enc'] = dms_row['acte_classant'].map(freq)
        expected_cols = ['duree_jours_init', 'jour_semaine_entree', 'mois_entree', 'age', 'sexe', 'dp',
                        'acte_type_simple', 'mode_entree', 'type_mode', 'acte_classant_enc']
        for col in expected_cols:
            if col not in dms_row.columns:
                dms_row[col] = df_diff_day[df_diff_day["acte_classant"] == ccam_code][col].iloc[0]
        missing_cols = set(preprocessor_diff.feature_names_in_) - set(dms_row.columns)
        for col in missing_cols:
            dms_row[col] = np.nan
        X_pred = preprocessor_diff.transform(dms_row)
        pred_log = stack_long.predict(X_pred)
        return round(float(np.expm1(pred_log[0])), 1), "jours"

    # Sinon, valeur externe
    dms_dg = dg.loc[dg["acte"] == ccam_code, "dms_globale"]
    if not dms_dg.empty:
        return round(float(dms_dg.iloc[0]), 1), "jours"

    dms_random = random.randint(1, 7)  # durée entre 1 et 7
    unite_random = random.choice(["heures", "jours"])
    return dms_random, unite_random

# ---------------------------
# Module 2 : génération planning
# ---------------------------
def generate_planning(patients_file: str,
                    chirurgiens_file: str,
                    lits_file: str,
                    inf_file: str,
                    anest_file: str,
                    salle_bloc_file: str, 
                    n_steps: int = 5):
    
    import pandas as pd, numpy as np, copy, ast, matplotlib.pyplot as plt, math, random

    # -----------------------------
    # 1. Charger les patients
    # -----------------------------
    df_patients = pd.read_csv(patients_file)
    df_patients = df_patients[df_patients['duree_sejour_predite'].notna()]

    def convertir_en_heures(row):
        return row['duree_sejour_predite']*24 if row['unite_duree_predite']=='jours' else row['duree_sejour_predite']

    df_patients['duree_sejour_heure'] = df_patients.apply(convertir_en_heures, axis=1)

    patients = []
    for idx, row in df_patients.iterrows():
        try:
            inf_dict = ast.literal_eval(str(row['infirmiere_sp']))
            if not isinstance(inf_dict, dict): inf_dict = {}
        except: inf_dict = {}
        nb_ibode = int(inf_dict.get('IBODE',0) or 0)
        nb_iade  = int(inf_dict.get('IADE',0) or 0)
        patients.append({
            'id': row['patient_id'],
            'age': row['age'],
            'ccam': row['ccam'],
            'dp': row['dp'],
            'specialite': str(row['chirurgien_sp']),
            'duree_intervention': row['duree_intervention'],
            'duree_sejour': row['duree_sejour_heure'],
            'chirurgien_sp': row['chirurgien_sp'],
            'anesthesistes': row['anesthesistes'],
            'nb_infirmiers_IBODE': nb_ibode,
            'nb_infirmiers_IADE': nb_iade
        })

    # -----------------------------
    # 2. Charger ressources
    # -----------------------------
    df_chir = pd.read_csv(chirurgiens_file)
    df_lits = pd.read_csv(lits_file)
    df_inf  = pd.read_csv(inf_file)
    df_anest= pd.read_csv(anest_file)
    

    lits = [str(l) for l in df_lits['lit'].tolist() if pd.notna(l)]
    df_salle_bloc = pd.read_csv(salle_bloc_file)
    nom_colonne_bloc = 'nom_bloc'  # à adapter selon ton CSV
    blocs_speciaux = df_salle_bloc[df_salle_bloc[nom_colonne_bloc].str.startswith('B1S')][nom_colonne_bloc].tolist()
    blocs_libres = df_salle_bloc[~df_salle_bloc[nom_colonne_bloc].str.startswith('B1S')][nom_colonne_bloc].tolist()


    # Chirurgiens
    chirurgiens = {}
    for idx,row in df_chir.iterrows():
        spe = str(row['Spécialité'][2:])
        chirs = ast.literal_eval(row['Chirurgien'])
        jours_dispo = [j.strip().replace('*','') for j in str(row['Jours de disponibilité']).split(',')]
        for chir in chirs:
            chirurgiens[chir] = {'spe': spe, 'jours_dispo': jours_dispo}

    # Infirmiers
    ibode_dict, iade_dict = {}, {}
    for idx,row in df_inf.iterrows():
        inf_id = row['Nom']
        inf_type = row['Spécialité']
        jours_dispo = [j.strip() for j in ast.literal_eval(f'"{row["Jours de disponibilité"]}"').split(',')]
        if inf_type.upper()=='IBODE': ibode_dict[inf_id] = {'jours_dispo': jours_dispo}
        if inf_type.upper()=='IADE':  iade_dict[inf_id]  = {'jours_dispo': jours_dispo}

    # Anesthésistes
    anesthesistes = {}
    for idx,row in df_anest.iterrows():
        anesthesistes[row['Nom']] = {
            'spe': row['Spécialité'],
            'jours_dispo': [j.strip() for j in ast.literal_eval(f'"{row["Jours de disponibilité"]}"').split(',')]
        }

    jours_semaine = ['Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi','Dimanche']

    # -----------------------------
    # 3. Solution initiale
    # -----------------------------
    solution_init, non_assignes = solution_initiale_stat(
        patients, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict, blocs_speciaux, blocs_libres
    )

    # -----------------------------
    # 4. Créer modèle et lancer optimisation
    # -----------------------------
    model = PlanningOptimizationModelStat(
        patients, lits, chirurgiens, anesthesistes, ibode_dict, iade_dict, blocs_speciaux,blocs_libres, solution_init, n_steps=n_steps
    )
    solution_finale_sma, best_cost, historique_couts_sma = model.run_model()

    return solution_finale_sma, best_cost, historique_couts_sma, non_assignes

# ---------------------------
# Module 3 : insertion nouveau patient
# ---------------------------

def add_patient_to_planning(
    solution_existante_file: str,
    patient_data: dict,
    chirurgiens_file: str,
    lits_file: str,
    inf_file: str,
    anest_file: str,
    salle_bloc_file: str,
    n_steps: int = 5
):
    # -----------------------------
    # 1. Charger le planning existant
    # -----------------------------
    df_planning = pd.read_csv(solution_existante_file, sep=',')
    solution_existante = []

    for idx, row in df_planning.iterrows():
        if pd.notna(row['heure_debut_intervention']):
            solution_existante.append({
                'id': row['id'],
                'heure_debut_intervention': row['heure_debut_intervention'],
                'salle': row['salle'],
                'chirurgien': row['chirurgien'],
                'lit': row['lit'],
                'anesthesistes': row['anesthesistes'],
                'infirmiers': ast.literal_eval(row['infirmiers']),
                'duree_intervention': row['duree_intervention'],
                'duree_sejour': row.get('duree_sejour', row['duree_intervention']),
                'specialite': row['chirurgien_sp'],
                'chirurgien_sp': row['chirurgien_sp'],
                'liste_inf': row['infirmiers'],
            })

    # -----------------------------
    # 2. Charger les ressources
    # -----------------------------
    df_chir = pd.read_csv(chirurgiens_file)
    df_lits = pd.read_csv(lits_file)
    lits = [str(l) for l in df_lits['lit'].tolist() if pd.notna(l)]
    df_inf = pd.read_csv(inf_file)
    df_anest = pd.read_csv(anest_file)

    # -----------------------------
    # 3. Charger les blocs depuis le fichier CSV
    # -----------------------------
    df_salle_bloc = pd.read_csv(salle_bloc_file)
    nom_colonne_bloc = 'nom_bloc'  # à adapter selon ton CSV
    blocs_speciaux = df_salle_bloc[df_salle_bloc[nom_colonne_bloc].str.startswith('B1S')][nom_colonne_bloc].tolist()
    blocs_libres = df_salle_bloc[~df_salle_bloc[nom_colonne_bloc].str.startswith('B1S')][nom_colonne_bloc].tolist()

    # -----------------------------
    # 4. Construire les dictionnaires
    # -----------------------------
    chirurgiens = {}
    for idx, row in df_chir.iterrows():
        spe = str(row['Spécialité'][2:])
        chirs = ast.literal_eval(row['Chirurgien'])
        jours_dispo = [j.strip().replace('*','') for j in str(row['Jours de disponibilité']).split(',')]
        for chir in chirs:
            chirurgiens[chir] = {
                'spe': spe,
                'jours_dispo': jours_dispo
            }

    ibode_dict = {}
    iade_dict = {}
    for idx, row in df_inf.iterrows():
        inf_id = row['Nom']
        inf_type = row['Spécialité']
        jours_dispo = [j.strip() for j in ast.literal_eval(f'"{row["Jours de disponibilité"]}"').split(',')]
        if inf_type.upper() == 'IBODE':
            ibode_dict[inf_id] = {'jours_dispo': jours_dispo}
        elif inf_type.upper() == 'IADE':
            iade_dict[inf_id] = {'jours_dispo': jours_dispo}

    anesthesistes = {}
    for idx, row in df_anest.iterrows():
        anesthesistes[row['Nom']] = {
            'spe': row['Spécialité'],
            'jours_dispo': [j.strip() for j in ast.literal_eval(f'"{row["Jours de disponibilité"]}"').split(',')]
        }

    # -----------------------------
    # 5. Ajouter le nouveau patient
    # -----------------------------
    patient = patient_data  # on prend le dictionnaire passé en argument
    # Conversion durée de séjour en heures si nécessaire
    if 'unite_duree_predite' in patient and patient['unite_duree_predite'].lower().startswith('j'):
        patient['duree_sejour'] = patient['duree_sejour'] * 24

    # -----------------------------
    # 6. Générer solution initiale et lancer le modèle
    # -----------------------------
    solution_init = solution_initiale_dynamique(
        patient, solution_existante, lits, chirurgiens,
        anesthesistes, ibode_dict, iade_dict, blocs_speciaux, blocs_libres
    )

    model = PlanningOptimizationModelDynamique(
        patient, lits, chirurgiens, anesthesistes,
        ibode_dict, iade_dict, solution_init,
        blocs_speciaux, blocs_libres, n_steps=n_steps
    )

    solution_finale_sma, cout_final_sma, historique_couts_sma = model.run_model()
    historique_cout = model.historique_couts_sma
    
    solution_gene = algorithme_genetique_insertion(
        patient, solution_existante,
        chirurgiens, ibode_dict, iade_dict,
        anesthesistes, lits, blocs_speciaux, blocs_libres,
        pop_size=10, generations=15
    )
    best_cost_gene = cout(solution_gene, anesthesistes, chirurgiens, ibode_dict, iade_dict)

    return {
        "solution_sma": solution_finale_sma,
        "cout_sma": cout_final_sma,
        "solution_gene": solution_gene,
        "cout_gene": best_cost_gene,
        "historique_sma": historique_couts_sma
    }




def clean_planning_csv(input_file: str, output_file: str):
    # Charger le CSV en ignorant les lignes vides
    df = pd.read_csv(input_file, sep=",", dtype=str, on_bad_lines="skip").fillna("")

    # 🔹 Supprimer les lignes d'en-tête répétées (celles qui commencent par "id" ou "heure_debut_intervention")
    df = df[df["id"] != "id"]

    # 🔹 Convertir les colonnes numériques quand c’est possible
    numeric_cols = ["heure_debut_intervention", "duree_intervention", "duree_sejour",
                    "age", "nb_anesthesistes", "nb_infirmiers_IBODE", "nb_infirmiers_IADE"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 🔹 Nettoyer la colonne infirmiers (convertir le texte en vraie liste Python)
    if "infirmiers" in df.columns:
        def parse_infirmiers(val):
            try:
                if isinstance(val, str) and val.startswith("["):
                    return ast.literal_eval(val)
                elif isinstance(val, str) and val != "":
                    return [val]
                return []
            except:
                return []
        df["infirmiers"] = df["infirmiers"].apply(parse_infirmiers)

    # 🔹 Supprimer les doublons (en convertissant les listes en chaînes pour comparaison)
    df_str = df.copy()
    if "infirmiers" in df_str.columns:
        df_str["infirmiers"] = df_str["infirmiers"].astype(str)
    df = df.loc[~df_str.duplicated(), :].copy()

    # 🔹 Réordonner les colonnes principales si présentes
    ordered_cols = [
        "id", "heure_debut_intervention", "salle", "chirurgien", "lit", "anesthesistes",
        "infirmiers", "duree_intervention", "duree_sejour", "specialite", "age", "ccam",
        "dp", "sexe", "nb_anesthesistes", "nb_infirmiers_IBODE", "nb_infirmiers_IADE",
        "unite_duree_predite"
    ]
    df = df[[c for c in ordered_cols if c in df.columns]]

    # 🔹 Sauvegarder le fichier nettoyé
    df.to_csv(output_file, index=False)
    print(f"✅ Fichier nettoyé sauvegardé sous : {output_file}")


def convertir_heure_en_date(heure):
    now = datetime.now()
    
    # Commencer la journée à 8h
    start_hour = 8
    end_hour = 18
    hours_per_day = end_hour - start_hour  # 10 heures ouvrables par jour
    
    # Combien de jours complets et d'heures restantes
    jours = int(heure // hours_per_day)
    heures_restantes = heure % hours_per_day
    
    # Calcul de l'heure finale
    date_base = now + timedelta(days=jours)
    heure_finale = start_hour + heures_restantes
    
    # Si dépasse 18h, passer au jour suivant
    if heure_finale >= end_hour:
        date_base += timedelta(days=1)
        heure_finale = start_hour + (heure_finale - end_hour)
    
    date_intervention = date_base.replace(hour=int(heure_finale), minute=0, second=0, microsecond=0)
    
    return date_intervention.strftime("%d/%m/%Y %H:%M")

def csv_to_solution(file_path):
    """
    Transforme un CSV sauvegardé en liste de dicts compatible solution_finale_sma / solution_gene
    """
    df = pd.read_csv(file_path)
    solution = []
    for _, row in df.iterrows():
        solution.append({
            'id': row['id'],
            'heure_debut_intervention': row.get('heure_debut_intervention', None),
            'salle': row.get('salle', None),
            'chirurgien': row.get('chirurgien', None),
            'lit': row.get('lit', None),
            'anesthesistes': row.get('anesthesistes', None),
            'infirmiers': ast.literal_eval(row['infirmiers']) if 'infirmiers' in row and pd.notna(row['infirmiers']) else [],
            'duree_intervention': row.get('duree_intervention', None),
            'duree_sejour': row.get('duree_sejour', row.get('duree_intervention', None)),
            'specialite': row.get('specialite', None),
            'chirurgien_sp': row.get('chirurgien_sp', None),
            'liste_inf': row.get('infirmiers', None),
        })
    return solution

# ---------------------------
# Fonction principale
# ---------------------------
if __name__ == "__main__":
    # ---------------------------
    # Charger les modèles de prédiction DMS
    # ---------------------------
    print("1. Charger les modèles de prédiction DMS...")
    pipeline_short, stack_long, preprocessor_short, preprocessor_diff = load_models_and_preprocessors()
    
    print("  Charger les bases pour la prédiction...")
    df =  pd.read_csv(os.path.join(DATA_DIR, 'AH_chir.csv'),sep=';')
    df_same_day =  df[df["date_entree"] == df["date_sortie"]].copy()
    df_diff_day = df[df["date_entree"] != df["date_sortie"]].copy()
    dg = pd.read_csv(os.path.join(DATA_DIR, 'ccam_pas_dans_la_base.csv'), sep=';')

    code_en_mot = pd.read_csv(os.path.join(DATA_DIR, 'ccam_en_mot.csv'), sep=';', encoding='utf-8')
    code_en_mot ['acte_classant'] = code_en_mot['Code']
    df = df.merge(code_en_mot, on="acte_classant", how="left")
    ccam_spe = pd.read_csv(os.path.join(DATA_DIR, 'ccam_spes.csv'), sep=';', encoding='utf-8')
    code_en_mot ['acte_classant'] = code_en_mot['Code'] 
    ccam_spe_dict = dict(zip(ccam_spe['acte_classant'], ccam_spe['specialite']))
    df['spe'] = df['acte_classant'].map(ccam_spe_dict)

    df['acte_type_simple'] = df['Spécialité'].apply(
        lambda x: (
            "Thérapeutique" if "THÉRAPEUTIQUES" in x else
            "Diagnostique" if "DIAGNOSTIQUES" in x else
            "Gestes complémentaires" if "GESTES COMPLÉMENTAIRES" in x else
            "Forfaits / actes transitoires" if "FORFAITS ET ACTES TRANSITOIRES" in x else
            "Suppléments" if "SUPPLÉMENTS" in x else
            "Radiothérapie externe" if "RADIOTHÉRAPIE EXTERNE" in x else
            "Autre"
        )
    )

    # --- Dates et features temporelles ---
    # faire 2 df différent (duree en heures, duree en jours)
    df["date_entree"] = pd.to_datetime(df["date_entree"])
    df["date_sortie"] = pd.to_datetime(df["date_sortie"])

    df_diff_day = df[df["date_entree"] != df["date_sortie"]].copy()

    df_diff_day['duree_jours_init'] = (df_diff_day['date_sortie'] - df_diff_day['date_entree']).dt.days
    df_diff_day['jour_semaine_entree'] = df_diff_day['date_entree'].dt.weekday
    df_diff_day['mois_entree'] = df_diff_day['date_entree'].dt.month

    df_same_day = df[df["date_entree"] == df["date_sortie"]].copy()

    # --- Features temporelles ---
    df_same_day['jour_semaine_entree'] = df_same_day['date_entree'].dt.weekday
    df_same_day['mois_entree'] = df_same_day['date_entree'].dt.month


    # --- Chargement de la base externe ---
    ccam_pas_dans_la_bdd = pd.read_csv(
        os.path.join(DATA_DIR, 'ccam_pas_dans_la_base.csv'),
        sep=';', encoding='utf-8'
    )
    ccam_pas_dans_la_bdd['acte'] = ccam_pas_dans_la_bdd['acte'].astype(str).str[:7]
    ccam_pas_dans_la_bdd['dms_globale'] = (
        ccam_pas_dans_la_bdd['dms_globale']
        .astype(str)
        .str.replace(',', '.')
        .astype(float)
    )
    
    print("2. Prédire la durée de séjour pour chaque patient...")
    df_patients = pd.read_csv(os.path.join(DATA_DIR, 'patients_bdd.csv'), sep=';')
    # Ajouter colonnes pour prédiction
    df_patients['duree_sejour_predite'] = None
    df_patients['unite_duree_predite'] = None

    # Boucler sur chaque patient
    for idx, row in df_patients.iterrows():
        pred, unite = predict_dms(
            patient_row=row,
            pipeline_short=pipeline_short,
            stack_long=stack_long,
            preprocessor_short=preprocessor_short,
            preprocessor_diff=preprocessor_diff,
            df_same_day=df_same_day,
            df_diff_day=df_diff_day,
            dg=dg
        )
        df_patients.at[idx, 'duree_sejour_predite'] = pred
        df_patients.at[idx, 'unite_duree_predite'] = unite
        if idx % 10 == 0:
            print(f"   ✅ Patient {idx+1}/{len(df_patients)} traité")
            
            
    # Chemin où tu veux sauvegarder le fichier
    output_file1 = os.path.join(DATA_DIR, 'patients_predicted.csv')

    # Sauvegarder le DataFrame avec les prédictions
    df_patients.to_csv(output_file1, index=False, sep=',')

    print(f" Fichier sauvegardé avec les patients et les durées de séjour prédites : {output_file1}") 
    # ---------------------------
    # 2️⃣ Générer le planning initial
    # ---------------------------
    print("3. Générer le planning initial...")
    patients_file = os.path.join(DATA_DIR, 'patients_predicted.csv')
    chirurgiens_file = os.path.join(DATA_DIR, 'chirurgiens.csv')
    lits_file = os.path.join(DATA_DIR, 'lits.csv')
    inf_file = os.path.join(DATA_DIR, 'inf.csv')
    anest_file = os.path.join(DATA_DIR, 'anest.csv')
    salle_bloc_file = os.path.join(DATA_DIR, 'Bloc_Salle.csv')

    df_chir = pd.read_csv(chirurgiens_file)
    df_lits = pd.read_csv(lits_file)
    lits = [str(l) for l in df_lits['lit'].tolist() if pd.notna(l)]
    df_inf= pd.read_csv(inf_file)
    df_anest= pd.read_csv(anest_file)
    
    blocs_speciaux = ['B1S1', 'B1S2']
    blocs_libres = ['B2S1','B2S2','B2S3','B3S1','B3S2','B3S3','B3S4','B4S1','B4S2','B5S1']

    chirurgiens = {}
    for idx, row in df_chir.iterrows():
        spe = str(row['Spécialité'][2:])
        chirs = ast.literal_eval(row['Chirurgien'])
        jours_dispo = [j.strip().replace('*','') for j in str(row['Jours de disponibilité']).split(',')]
        for chir in chirs:
            chirurgiens[chir] = {
                'spe': spe,
                'jours_dispo': jours_dispo
            }
            
    ibode_dict = {}
    iade_dict = {}

    for idx, row in df_inf.iterrows():
        inf_id = row['Nom']  # ou un ID unique si tu as
        inf_type = row['Spécialité']  # doit être 'IBODE' ou 'IADE'
        jours_dispo = [j.strip() for j in ast.literal_eval(f'"{row["Jours de disponibilité"]}"').split(',')]

        if inf_type.upper() == 'IBODE':
            ibode_dict[inf_id] = {
                'jours_dispo': jours_dispo
            }
        elif inf_type.upper() == 'IADE':
            iade_dict[inf_id] = {
                'jours_dispo': jours_dispo
            }


    anesthesistes = {}
    for idx, row in df_anest.iterrows():
        anesthesistes[row['Nom']] = {
            'spe': row['Spécialité'],
            'jours_dispo': [j.strip() for j in ast.literal_eval(f'"{row["Jours de disponibilité"]}"').split(',')]
        }
        

    solution_finale_sma_stat, best_cost, historique_couts_sma, non_assignes = generate_planning(
        patients_file,
        chirurgiens_file,
        lits_file,
        inf_file,
        anest_file,
        salle_bloc_file,
        n_steps=5
    )

    print("✅ Planning initial généré")
    print("Coût initial :", best_cost)
    print("Patients non assignés :", non_assignes)
    

    
    output_file2 = os.path.join(DATA_DIR, 'patients_predicted_planifie.csv')
    
    
    # Vérification du type avant la sauvegarde
    if isinstance(solution_finale_sma_stat, list):
        df_solution = pd.DataFrame(solution_finale_sma_stat)
    elif isinstance(solution_finale_sma_stat, dict):
        df_solution = pd.DataFrame([solution_finale_sma_stat])
    else:
        raise TypeError("❌ La solution finale n'est ni une liste ni un dictionnaire, impossible de sauvegarder.")

    df_solution.to_csv(output_file2, index=False, sep=',')
    print(f" Fichier sauvegardé avec les patients prédits planinifiés: {output_file2}")
    
    # ---------------------------
    # 3️⃣ Ajouter un nouveau patient
    # ---------------------------
    print("4. Ajouter un nouveau patient...")

    # Charger le planning existant pour déterminer le prochain ID
    df_planning_exist = pd.read_csv(os.path.join(DATA_DIR, 'patients_predicted_planifie.csv'), sep=',')

    # Calculer le prochain ID
    existing_ids = [int(row.replace('P','')) for row in df_planning_exist['id'] if str(row).startswith('P')]
    next_id = max(existing_ids)+1 if existing_ids else 1
    next_id_str = f"P{next_id}"

    # Saisie des informations patient via terminal
    age = input("Âge du patient : ")
    ccam = input("Code CCAM : ")
    dp = input("Type d'acte (dp) : ")
    sexe = input("Sexe : ")
    chir_sp = input("Spécialité du chirurgien : ")
    specialite = chir_sp
    duree_intervention = float(input("Durée de l'intervention (heures) : "))
    nb_anest = int(input("Nombre d'anesthésistes : "))
    nb_ibode = int(input("Nombre d'IBODE : "))
    nb_iade = int(input("Nombre d'IADE : "))

    # Créer le dictionnaire du nouveau patient
    nouveau_patient = {
        'id': next_id_str,
        'age': int(age),
        'ccam': ccam,
        'dp': dp,
        'sexe': sexe,
        'chirurgien_sp': chir_sp,
        'specialite': specialite,
        'duree_intervention': duree_intervention,
        'nb_anesthesistes': nb_anest,
        'nb_infirmiers_IBODE': nb_ibode,
        'nb_infirmiers_IADE': nb_iade,
    }

    print(f"🆔 Nouveau patient enregistré avec l'ID : {next_id_str}")
    
    # 🔹 Prédiction DMS pour la durée de séjour
    pred_dms, unite = predict_dms(
        nouveau_patient, pipeline_short, stack_long,
        preprocessor_short, preprocessor_diff,
        df_same_day, df_diff_day, dg
    )
    if pred_dms:
        if unite == "jours":
            nouveau_patient['duree_sejour'] = pred_dms * 24  # convertir en heures
        else:
            nouveau_patient['duree_sejour'] = pred_dms
        nouveau_patient['unite_duree_predite'] = unite

    print(f"⏱ Durée de séjour prédite : {nouveau_patient['duree_sejour']} ({nouveau_patient['unite_duree_predite']})")

    # Prévoir la durée exacte via prédiction DMS si nécessaire
    pred_dms, unité = predict_dms(
        nouveau_patient, pipeline_short, stack_long,
        preprocessor_short, preprocessor_diff,
        df_same_day, df_diff_day, dg
    )
    if pred_dms:
        if unité == "jours":
            nouveau_patient['duree_sejour'] = pred_dms * 24
        else:
            nouveau_patient['duree_sejour'] = pred_dms

    resultats = add_patient_to_planning(
        solution_existante_file= os.path.join(DATA_DIR, 'patients_predicted_planifie.csv'),
        patient_data=nouveau_patient,
        chirurgiens_file=chirurgiens_file,
        lits_file=lits_file,
        inf_file=inf_file,
        anest_file=anest_file,
        salle_bloc_file=salle_bloc_file,
        n_steps=5
    )
    
    historique_cout = resultats['historique_sma']
    solution_finale_sma = resultats['solution_sma']
    solution_finale_gene = resultats['solution_gene']
    
    
    pd.DataFrame(solution_finale_sma).to_csv(os.path.join(DATA_DIR, 'patients_predicted_planifie_sma.csv'), index=False)
    pd.DataFrame(solution_finale_gene).to_csv(os.path.join(DATA_DIR, 'patients_predicted_planifie_gene.csv'), index=False)
    
    
    patient_sma = next(item for item in solution_finale_sma if item['id'] == next_id_str)
    date_rdv_sma = patient_sma['heure_debut_intervention']
    print(f" Date de rendez-vous proposé par le sma pour le patient d'id : {next_id_str} est : {convertir_heure_en_date(date_rdv_sma)}")
    
    patient_gene = next(item for item in solution_finale_gene if item['id'] == next_id_str)
    date_rdv_gene = patient_gene['heure_debut_intervention']
    print(f" Date de rendez-vous proposé par le génétique pour le patient d'id : {next_id_str} est : {convertir_heure_en_date(date_rdv_gene)}")

    
    input_file_sma=os.path.join(DATA_DIR, 'patients_predicted_planifie_sma.csv')
    output_file_sma=os.path.join(DATA_DIR, 'patients_predicted_planifie_sma_clean.csv')
    
    input_file_gene=os.path.join(DATA_DIR, 'patients_predicted_planifie_gene.csv')
    output_file_gene=os.path.join(DATA_DIR, 'patients_predicted_planifie_gene_clean.csv')
    
    clean_planning_csv(
        input_file_sma,
        output_file_sma
    )
    clean_planning_csv(
        input_file_gene,
        output_file_gene
    )
    
    print("✅ Nouveau patient ajouté")
    print(f" Fichier sauvegardé où on a ajouté un patient par le sma: {output_file_sma}")
    print(f" Fichier sauvegardé où on a ajouté un patient par le génétique: {output_file_gene}")
    print("Coût final après ajout sma:", resultats['cout_sma'])
    print("Coût final après ajout génétique:", resultats['cout_gene'])
    
    
    
    
    # GRAPHES
        # ---------------------------------------------------
        # 🎨 Couleurs selon violation
        # ---------------------------------------------------
    def couleur_violation(p, solution, nouvel_id=None):
        """
        Retourne la couleur d’un patient :
        🔴 rouge si violation
        💗 rose si c’est le patient ajouté
        🔵 sinon
        """
        # Nouveau patient → rose
        if nouvel_id is not None and p.get('id') == nouvel_id:
            return 'pink'
        
        # Violations → rouge
        if not respect_salle(p, solution): return 'red'
        if not respect_chir(p, solution): return 'red'
        if not respect_lit(p, solution): return 'red'
        
        # OK → bleu
        return 'royalblue'


    # ---------------------------------------------------
    # 🛏️ Gantt - Lits
    # ---------------------------------------------------
    plt.figure(figsize=(14, 8))
    for p in solution_finale_sma:
        debut_j = (p['heure_debut_intervention'] + p['duree_intervention']) / 24
        fin_j = debut_j + p['duree_sejour'] / 24
        couleur = couleur_violation(p, solution_finale_sma, nouvel_id=next_id_str)

        plt.barh(p['lit'], fin_j - debut_j, left=debut_j,
                color=couleur, edgecolor='black', alpha=0.8)
        plt.text(debut_j + (fin_j - debut_j) / 2, p['lit'], f"{p['id']}",
                va='center', ha='center', color='white',
                fontsize=8, fontweight='bold', rotation=90)

    plt.xlabel("Jours")
    plt.ylabel("Lits")
    plt.title("Gantt - Occupation des lits - SMA(Rouge = violation, Rose = nouveau patient)")
    plt.grid(True, axis='x', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.show()
    


    # ---------------------------------------------------
    # 🛏️ Gantt - Lits -GENE
    # ---------------------------------------------------
    plt.figure(figsize=(14, 8))
    for p in solution_finale_gene:
        debut_j = (p['heure_debut_intervention'] + p['duree_intervention']) / 24
        fin_j = debut_j + p['duree_sejour'] / 24
        couleur = couleur_violation(p, solution_finale_gene, nouvel_id=next_id_str)

        plt.barh(p['lit'], fin_j - debut_j, left=debut_j,
                color=couleur, edgecolor='black', alpha=0.8)
        plt.text(debut_j + (fin_j - debut_j) / 2, p['lit'], f"{p['id']}",
                va='center', ha='center', color='white',
                fontsize=8, fontweight='bold', rotation=90)

    plt.xlabel("Jours")
    plt.ylabel("Lits")
    plt.title("Gantt - Occupation des lits - Génétique(Rouge = violation, Rose = nouveau patient)")
    plt.grid(True, axis='x', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.show()
    
    


    # ---------------------------------------------------
    # 👨‍⚕️ Gantt - Chirurgiens
    # ---------------------------------------------------
    plt.figure(figsize=(14, 8))
    specialites = list(set([p['specialite'] for p in solution_finale_sma]))
    couleurs_spe = plt.cm.tab20(np.linspace(0, 1, len(specialites)))
    spe_to_couleur = {spe: couleurs_spe[i] for i, spe in enumerate(specialites)}

    for p in solution_finale_sma:
        chir = p['chirurgien']
        spe = p['specialite']
        debut_interv = p['heure_debut_intervention']
        fin_interv = debut_interv + p['duree_intervention']
        couleur = couleur_violation(p, solution_finale_sma, nouvel_id=next_id_str)

        plt.barh(chir, fin_interv - debut_interv, left=debut_interv,
                color=couleur, edgecolor='black', alpha=0.85)
        plt.text(debut_interv + (fin_interv - debut_interv) / 2, chir,
                f"{p['id']} ({p['salle']})",
                va='center', ha='center', color='white',
                fontsize=8, fontweight='bold')

    plt.xlabel("Heures")
    plt.ylabel("Chirurgiens")
    plt.title("Gantt - Activité des chirurgiens - SMA(Rouge = violation, Rose = nouveau patient)")
    plt.grid(True, axis='x', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.show()


    # ---------------------------------------------------
    # 📈 Occupation des blocs
    # ---------------------------------------------------
    nb_jours = 30
    occupation_salles_jour = [0] * nb_jours
    for p in solution_finale_sma:
        debut_j = int(p['heure_debut_intervention'] // 24)
        fin_j = int(math.ceil((p['heure_debut_intervention'] + p['duree_intervention']) / 24))
        for j in range(debut_j, fin_j):
            occupation_salles_jour[j] += 1

    plt.figure(figsize=(12, 5))
    plt.plot(range(nb_jours), occupation_salles_jour, marker='o', color='orange')
    plt.xlabel("Jour")
    plt.ylabel("Salles occupées")
    plt.title("Occupation des blocs opératoires par jour SMA")
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.show()


    # ---------------------------------------------------
    # 📊 Occupation des lits
    # ---------------------------------------------------
    nb_jours = math.ceil(max(p['heure_debut_intervention'] +
                            p['duree_intervention'] +
                            p['duree_sejour'] for p in solution_finale_sma) / 24)
    occupation_lits_jour = [0] * nb_jours

    for p in solution_finale_sma:
        debut_j = int((p['heure_debut_intervention'] + p['duree_intervention']) // 24)
        fin_j = int(math.ceil((p['heure_debut_intervention'] +
                            p['duree_intervention'] +
                            p['duree_sejour']) / 24))
        for j in range(debut_j, fin_j):
            occupation_lits_jour[j] += 1

    plt.figure(figsize=(12, 5))
    plt.plot(range(nb_jours), occupation_lits_jour, marker='o', color='royalblue')
    plt.xlabel("Jour")
    plt.ylabel("Lits occupés")
    plt.title("Occupation des lits par jour SMA")
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.show()


    # ---------------------------------------------------
    # 👩‍⚕️ Gantt - Infirmiers
    # ---------------------------------------------------
    plt.figure(figsize=(14, 8))
    tous_infs = list(ibode_dict.keys()) + list(iade_dict.keys())

    for inf in tous_infs:
        interventions_inf = [p for p in solution_finale_sma if inf in p.get('infirmiers', [])]
        for p in interventions_inf:
            debut = p['heure_debut_intervention']
            fin = debut + p['duree_intervention']
            couleur = couleur_violation(p, solution_finale_sma, nouvel_id=next_id_str)

            plt.barh(inf, fin - debut, left=debut,
                    color=couleur, edgecolor='black', alpha=0.85)
            plt.text(debut + (fin - debut) / 2, inf, f"{p['id']}",
                    va='center', ha='center', color='white',
                    fontsize=8, fontweight='bold')

    plt.xlabel("Heures")
    plt.ylabel("Infirmiers (IBODE/IADE)")
    plt.title("Gantt - Activité des infirmiers -SMA (Rouge = violation, Rose = nouveau patient)")
    plt.grid(True, axis='x', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.show()


    # ---------------------------------------------------
    # 🩺 Gantt - Anesthésistes
    # ---------------------------------------------------
    plt.figure(figsize=(14, 8))
    for anesth in anesthesistes.keys():
        interventions_anesth = [p for p in solution_finale_sma if p.get('anesthesistes') == anesth]
        for p in interventions_anesth:
            debut = p['heure_debut_intervention']
            fin = debut + p['duree_intervention']
            couleur = couleur_violation(p, solution_finale_sma, nouvel_id=next_id_str)

            plt.barh(anesth, fin - debut, left=debut,
                    color=couleur, edgecolor='black', alpha=0.85)
            plt.text(debut + (fin - debut) / 2, anesth, f"{p['id']}",
                    va='center', ha='center', color='white',
                    fontsize=8, fontweight='bold')

    plt.xlabel("Heures")
    plt.ylabel("Anesthésistes")
    plt.title("Gantt - Activité des anesthésistes - SMA (Rouge = violation, Rose = nouveau patient)")
    plt.grid(True, axis='x', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.show()