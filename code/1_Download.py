import biobricks as bb
import numpy as np
import dvc.api
import os

bb.install('chemharmony')
chemharmony = bb.load('chemharmony')
activities = chemharmony.activities.read().to_pandas()[['smiles','pid','value']]

activities = activities.dropna()
activities = activities.replace(['negative', 'Negative', 'inactive', 'Inactive'], 0)
activities = activities.replace(['positive', 'Positive', 'Active', 'active antagonist', 'active agonist'], 1)
activities = activities.replace(['quartile_1', 'quartile_2', 'quartile_3', 'quartile_4',], 1)
activities = activities[ activities['smiles'].str.len() <= 244]
activities.columns = ['smiles','assay','value']

params = dvc.api.params_show()
outFolder = params['download']['outFolder']
os.makedirs(outFolder, exist_ok=True)

activities.to_csv('{}RawChemHarmony.csv'.format(outFolder))