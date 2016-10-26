import sys
sys.path.append('./thrift/gen-py')
sys.path.append('./events')
from thrift import Thrift
from thrift.transport import TSocket
from thrift.transport import TTransport
from thrift.protocol import TBinaryProtocol

# modeldb imports
from events import *
import GridCrossValidation 
from modeldb import ModelDBService
import modeldb.ttypes as modeldb_types

# sklearn imports
import numpy as np
import pandas as pd
from sklearn.linear_model import *
from sklearn.preprocessing import *
from sklearn.pipeline import Pipeline
from sklearn.grid_search import GridSearchCV
import sklearn.metrics

#Overrides the fit function for all models except for Pipeline and GridSearch Cross Validation, which have their own functions.
def fitFn(self,X,y=None):
    df = X
    #Certain fit functions only accept one argument
    if y is None:
        models = self.fit(X)
    else:
        models = self.fit(X,y)
        yDf = pd.DataFrame(y)
        if type(X) is pd.DataFrame:
            df = X.join(yDf)
        else:
            #if X does not have column-names, we cannot perform a join, and must instead add a new column.
            df = pd.DataFrame(X)
            df['outputColumn'] = y
    #Calls FitEvent in other class and adds to buffer 
    fitEvent = FitEvent(models, self, df)
    Syncer.instance.addToBuffer(fitEvent)

#Overrides the predict function for models, provided that the predict function takes in one argument
def predictFn(self, X):
    predictArray = self.predict(X)
    predictDf = pd.DataFrame(predictArray)
    newDf = X.join(predictDf)
    predictEvent = TransformEvent(X, newDf, self)
    Syncer.instance.addToBuffer(predictEvent)
    return predictArray

#Overrides the transform function for models, provided that the transform function takes in one argument
def transformFn(self, X):
    transformedOutput = self.transform(X)
    if type(transformedOutput) is np.ndarray:
        newDf = pd.DataFrame(transformedOutput)
    else:
        newDf = pd.DataFrame(transformedOutput.toarray())
    transformEvent = TransformEvent(X, newDf, self)
    Syncer.instance.addToBuffer(transformEvent)
    return transformedOutput

#Overrides the Pipeline model's fit function
def fitFnPipeline(self,X,y):
    #Check if pipeline contains valid estimators and transformers
    checkValidPipeline(self.steps)

    #Make Fit Event for overall pipeline
    pipelineModel = self.fit(X,y)
    pipelineFit = FitEvent(pipelineModel, self, X)

    #Extract all the estimators from pipeline
    #All estimators call 'fit' and 'transform' except the last estimator (which only calls 'fit')
    names, sk_estimators = zip(*self.steps)
    estimators = sk_estimators[:-1]
    lastEstimator = sk_estimators[-1]

    transformStages = []
    fitStages = []
    curDataset = X

    for index, estimator in enumerate(estimators):
        oldDf = curDataset
        model = estimator.fit(oldDf, y)
        transformedOutput = model.transform(oldDf)

        #Convert transformed output into a proper pandas DataFrame object
        if type(transformedOutput) is np.ndarray:
            newDf = pd.DataFrame(transformedOutput)
        else:
            newDf = pd.DataFrame(transformedOutput.toarray())

        curDataset = transformedOutput

        #populate the stages
        transformEvent = TransformEvent(oldDf, newDf, model)
        transformStages.append((index, transformEvent))
        fitEvent = FitEvent(model, estimator, oldDf)
        fitStages.append((index, fitEvent))

    #Handle last estimator, which has a fit method (and may not have transform)
    oldDf = curDataset
    model = lastEstimator.fit(oldDf, y)
    fitEvent = FitEvent(model, estimator, oldDf)
    fitStages.append((index+1, fitEvent))

    #Create the pipeline event with all components
    pipelineEvent = PipelineEvent(pipelineFit, transformStages, fitStages)

    Syncer.instance.addToBuffer(pipelineEvent)

#Helper function to check whether a pipeline is constructed properly. Taken from original sklearn pipeline source code with minor modifications, which are commented below.
def checkValidPipeline(steps):
    names, estimators = zip(*steps)
    transforms = estimators[:-1]
    estimator = estimators[-1]

    for t in transforms:
        #Change from original scikit: checking for "fit" and "transform" methods, rather than "fit_transform" as each event is logged separately to database
        if (not (hasattr(t, "fit")) and hasattr(t, "transform")):
            raise TypeError("All intermediate steps of the chain should "
                            "be transforms and implement fit and transform"
                            " '%s' (type %s) doesn't)" % (t, type(t)))

    if not hasattr(estimator, "fit"):
        raise TypeError("Last step of chain should implement fit "
                        "'%s' (type %s) doesn't)"
                        % (estimator, type(estimator)))

def fitFnGridSearch(self, X,y):
    GridCrossValidation.fit(self,X,y)
    [inputDataFrame, crossValidations, seed, evaluator, bestModel, bestEstimator, numFolds] = self.gridCVevent

    #Calls SyncGridCVEvent and adds to buffer.
    gridEvent = GridSearchCVEvent(inputDataFrame, crossValidations, seed, evaluator, bestModel, bestEstimator, numFolds)
    Syncer.instance.addToBuffer(gridEvent)

# Stores object with associated tagName
def addTagObject(self, tagName):
    Syncer.instance.storeTagObject(id(self), tagName)

class NewOrExistingProject:
    def __init__(self, name, author, description):
        self.name = name
        self.author = author
        self.description = description

    def toThrift(self):
        return modeldb_types.Project(-1, self.name, self.author, self.description)

class ExistingProject:
    def __init__(self, id):
        self.id = id

    def toThrift(self):
        return modeldb_types.Project(self.id, "", "", "")

class ExistingExperiment:
    def __init__(self, id):
        self.id = id

    def toThrift(self):
        return modeldb_types.Experiment(self.id, -1, "", "", False)

class DefaultExperiment:
    def toThrift(self):
        return modeldb_types.Experiment(-1, -1, "", "", True)

class NewOrExistingExperiment:
    def __init__(self, name, description):
        self.name = name
        self.description = description

    def toThrift(self):
        return modeldb_types.Experiment(-1, -1, self.name, self.description, False)

class NewExperimentRun:
    def __init__(self, description=""):
        self.description = description

    def toThrift(self):
        return modeldb_types.ExperimentRun(-1, -1, self.description)

class ExistingExperimentRun:
    def __init__(id):
        self.id = id

    def toThrift():
        return modeldb_types.ExperimentRun(self.id, -1, "")

class Syncer(object):
    instance = None
    def __new__(cls, projectConfig, experimentConfig, experimentRunConfig): # __new__ always a classmethod
        # This will break if cls is some random class.
        if not cls.instance:
            cls.instance = object.__new__(cls, projectConfig, experimentConfig, experimentRunConfig)
        return cls.instance

    def __init__(self, projectConfig, experimentConfig, experimentRunConfig):
        self.idForObject = {}
        self.objectForId = {}
        self.tagForObject = {}
        self.objectForTag = {}
        self.bufferList = []
        self.initializeThriftClient()
        self.enableSyncFunctions()
        self.addTags()
        self.setup(projectConfig, experimentConfig, experimentRunConfig)

    def setup(self, projectConfig, experimentConfig, experimentRunConfig):
        self.setProject(projectConfig)
        self.setExperiment(experimentConfig)
        self.setExperimentRun(experimentRunConfig)

    def __str__(self):
        return "Syncer"

    def setProject(self, projectConfig):
        self.project = projectConfig.toThrift()
        # TODO: can we clean up this construct: SyncableBlah.syncblah
        projectEvent = ProjectEvent(self.project)
        projectEvent.sync(self)

    def setExperiment(self, experimentConfig):
        self.experiment = experimentConfig.toThrift()
        self.experiment.projectId = self.project.id
        experimentEvent = ExperimentEvent(
            self.experiment)
        experimentEvent.sync(self)

    def setExperimentRun(self, experimentRunConfig):
        self.experimentRun = experimentRunConfig.toThrift()
        self.experimentRun.experimentId = self.experiment.id
        experimentRunEvent = \
          ExperimentRunEvent(self.experimentRun)
        experimentRunEvent.sync(self)

    def storeObject(self, obj, Id):
        self.idForObject[obj] = Id
        self.objectForId[Id] = obj

    def storeTagObject(self, obj, tag):
        self.tagForObject[obj] = tag
        self.objectForTag[tag] = obj

    def addToBuffer(self, event):
        self.bufferList.append(event)

    def sync(self):
        for b in self.bufferList:
            b.sync(self)

    def setColumns(self, df):
        if type(df) is pd.DataFrame:
            columns = df.columns.values
            if type(columns) is np.ndarray:
                columns = np.array(columns).tolist()
            for i in range(0, len(columns)):
                columns[i] = str(columns[i])
        else:
            columns = []
        return columns

    def setDataFrameSchema(self, df):
        dataFrameCols = []
        columns = self.setColumns(df)
        for i in range(0, len(columns)):
            columnName = str(columns[i])
            dfc = modeldb_types.DataFrameColumn(columnName, str(df.dtypes[i]))
            dataFrameCols.append(dfc)
        return dataFrameCols

    def convertModeltoThrift(self, model):
        tid = -1
        tag = ""
        if model in self.idForObject:
            tid = self.idForObject[model]
        if id(model) in self.tagForObject:
            tag = self.tagForObject[id(model)]
        transformerType = model.__class__.__name__
        t = modeldb_types.Transformer(tid, [0.0], transformerType, tag)
        return t

    def convertDftoThrift(self, df):
        tid = -1
        tag = ""
        dfImm = id(df)
        if dfImm in self.idForObject:
            tid = self.idForObject[dfImm]
        if dfImm in self.tagForObject:
            tag = self.tagForObject[dfImm]
        dataFrameColumns = self.setDataFrameSchema(df)
        modeldbDf = modeldb_types.DataFrame(tid, dataFrameColumns, df.shape[0], tag)
        return modeldbDf

    def convertSpectoThrift(self, spec, df):
        tid = -1
        tag = ""
        if spec in self.idForObject:
            tid = self.idForObject[spec]
        if id(spec) in self.tagForObject:
            tag = self.tagForObject[id(spec)]
        columns = self.setColumns(df)
        hyperparams = []
        params = spec.get_params()
        for param in params:
            hp = modeldb_types.HyperParameter(param, str(params[param]), type(params[param]).__name__, sys.float_info.min, sys.float_info.max)
            hyperparams.append(hp)
        ts = modeldb_types.TransformerSpec(tid, spec.__class__.__name__, columns, hyperparams, tag)
        return ts

    def initializeThriftClient(self, host="localhost", port=6543):
        # Make socket
        transport = TSocket.TSocket(host, port)

        # Buffering is critical. Raw sockets are very slow
        transport = TTransport.TFramedTransport(transport)

        # Wrap in a protocol
        protocol = TBinaryProtocol.TBinaryProtocol(transport)

        # Create a client to use the protocol encoder
        self.client = ModelDBService.Client(protocol)
        transport.open()

    # Adds tag as a method to objects, allowing users to tag objects with their own description
    def addTags(self):
        setattr(pd.DataFrame, "tag", addTagObject)
        models = [LogisticRegression, LinearRegression, LabelEncoder, OneHotEncoder,
                        Pipeline, GridSearchCV]
        for class_name in models:
            setattr(class_name, "tag", addTagObject)

    #This function extends the scikit classes to implement custom *Sync versions of methods. (i.e. fitSync() for fit())
    #Users can easily add more models to this function.
    def enableSyncFunctions(self):
        #Linear Models (transform has been deprecated)
        for class_name in [LogisticRegression, LinearRegression]:
            setattr(class_name, "fitSync", fitFn)
            setattr(class_name, "predictSync", predictFn)

        #Preprocessing models
        for class_name in [LabelEncoder, OneHotEncoder]:
            setattr(class_name, "fitSync", fitFn)
            setattr(class_name, "transformSync", transformFn)

        #Pipeline model
        for class_name in [Pipeline]:
            setattr(class_name, "fitSync", fitFnPipeline)

        #Grid-Search Cross Validation model
        for class_name in [GridSearchCV]:
            setattr(class_name, "fitSync",  fitFnGridSearch)