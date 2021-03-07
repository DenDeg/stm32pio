import pathlib
from typing import List, Iterator, Mapping, Any, Callable

from PySide2.QtCore import QAbstractListModel, Signal, Slot, QObject, QThreadPool, QModelIndex, Qt, QUrl

from stm32pio.gui.project import ProjectListItem
from stm32pio.gui.util import Worker, ProjectID
from stm32pio.gui.log import module_logger
import stm32pio.gui.settings


class ProjectsList(QAbstractListModel):
    """
    QAbstractListModel implementation - describe basic operations and delegate all main functionality to the
    ProjectListItem
    """

    goToProject = Signal(int, arguments=['indexToGo'])

    def __init__(self, projects: List[ProjectListItem] = None, parent: QObject = None):
        """
        Args:
            projects: initial list of projects
            parent: QObject to be parented to
        """
        super().__init__(parent=parent)

        self.projects = projects if projects is not None else []

        self.workers_pool = QThreadPool(parent=self)
        self.workers_pool.setMaxThreadCount(1)  # only 1 active worker at a time
        self.workers_pool.setExpiryTimeout(-1)  # tasks wait forever for the available spot

    @Slot(int, result=ProjectListItem)
    def get(self, index: int):
        """
        Expose the ProjectListItem to the GUI QML side. You should firstly register the returning type using
        qmlRegisterType or similar
        """
        if index in range(len(self.projects)):
            return self.projects[index]

    def rowCount(self, parent=None, *args, **kwargs):
        return len(self.projects)

    def data(self, index: QModelIndex, role=None):
        if role == Qt.DisplayRole or role is None:
            return self.projects[index.row()]

    def _saveInSettings(self) -> None:
        """
        Get correct projects and save them to Settings. Intended to be run in a thread (as it blocks)
        """

        # Wait for all projects to be loaded (project.init_project is finished), whether successful or not
        while not all(project.name != 'Loading...' for project in self.projects):
            pass

        # Only correct ones (inner Stm32pio instance has been successfully constructed)
        projects_to_save = [project for project in self.projects if project.project is not None]

        settings = stm32pio.gui.settings.global_instance()
        settings.beginGroup('app')
        settings.remove('projects')  # clear the current saved list
        settings.beginWriteArray('projects')
        for idx, project in enumerate(projects_to_save):
            settings.setArrayIndex(idx)
            # This ensures that we always save paths in pathlib form
            settings.setValue('path', str(project.project.path))
        settings.endArray()
        settings.endGroup()

        module_logger.info(f"{len(projects_to_save)} projects have been saved to Settings")  # total amount

    def saveInSettings(self) -> None:
        """Spawn a thread to wait for all projects and save them in background"""
        self.workers_pool.start(Worker(self._saveInSettings, logger=module_logger, parent=self))

    def each_project_is_duplicate_of(self, path: str) -> Iterator[bool]:
        """
        Returns generator yielding an answer to the question "Is current project is a duplicate of one represented by a
        given path?" for every project in this model, one by one.

        Logic explanation: At a given time some projects (e.g., when we add a bunch of projects, recently added ones)
        can be not instantiated yet so we cannot extract their project.path property and need to check before comparing.
        In this case, simply evaluate strings. Also, samefile will even raise, if the given path doesn't exist and
        that's exactly what we want.
        """
        for list_item in self.projects:
            try:
                yield (list_item.project is not None and list_item.project.path.samefile(pathlib.Path(path))) or \
                      path == list_item.name  # simply check strings if a path isn't available
            except OSError:
                yield False

    def addListItem(self, path: str, list_item_kwargs: Mapping[str, Any] = None,
                    on_initialized: Callable[[ProjectID], None] = None) -> ProjectListItem:
        """
        Create and append to the list tail a new ProjectListItem instance. This doesn't save in QSettings, it's an up to
        the caller task (e.g. if we adding a bunch of projects, it make sense to store them once in the end).

        Args:
            path: path as string
            list_item_kwargs: keyword arguments passed to the ProjectListItem constructor
            on_initialized: optional callback to run after the complete project initialization
        """

        # Shallow copy, dict makes it mutable
        list_item_kwargs = dict(list_item_kwargs if list_item_kwargs is not None else {})

        # Parent is always this model so we implicitly pass it there
        if 'parent' not in list_item_kwargs or not list_item_kwargs['parent']:
            list_item_kwargs['parent'] = self

        duplicate_index = next((idx for idx, is_duplicated in enumerate(self.each_project_is_duplicate_of(path))
                                if is_duplicated), -1)
        if duplicate_index > -1:
            # Just added project is already in the list so abort the addition
            module_logger.warning(f"This project is already in the list: {path}")

            # If some parameters were provided, merge them
            proj_params = list_item_kwargs.get('project_kwargs', {}).get('parameters', {})
            if len(proj_params):
                self.projects[duplicate_index].logger.info(f"updating parameters from the CLI... {proj_params}")
                # Note: will save stm32pio.ini even if there was not one
                self.projects[duplicate_index].run('save_config', [proj_params])

            self.goToProject.emit(duplicate_index)  # jump to the existing one

            return self.projects[duplicate_index]
        else:
            # Insert given path into the constructor args (do not use dict.update() as we have list value that we also
            # want to "merge")
            if 'project_args' not in list_item_kwargs or len(list_item_kwargs['project_args']) == 0:
                list_item_kwargs['project_args'] = [path]
            else:
                list_item_kwargs['project_args'][0] = path

            # The project is ready to be appended to the model right after the main constructor (wrapper) finished.
            # The underlying Stm32pio class will be initialized soon later in the dedicated thread
            project = ProjectListItem(**list_item_kwargs)

            if on_initialized is not None:
                project.initialized.connect(on_initialized)

            self.beginInsertRows(QModelIndex(), self.rowCount(), self.rowCount())
            self.projects.append(project)
            self.endInsertRows()

            return project


    @Slot('QStringList')
    def addProjectsByPaths(self, paths: List[str]):
        """QUrl path (typically is sent from the QML GUI)"""
        if len(paths):
            for path_str in paths:  # convert to strings
                path_qurl = QUrl(path_str)
                if path_qurl.isEmpty():
                    module_logger.warning(f"Given path is empty: {path_str}")
                    continue
                elif path_qurl.isLocalFile():  # file://...
                    path: str = path_qurl.toLocalFile()
                elif path_qurl.isRelative():  # this means that the path string is not starting with 'file://' prefix
                    path: str = path_str  # just use a source string
                else:
                    module_logger.error(f"Incorrect path: {path_str}")
                    continue
                self.addListItem(path)
            self.saveInSettings()  # save after all
        else:
            module_logger.warning("No paths were given")


    @Slot(int)
    def removeProject(self, index: int):
        """
        Remove the project residing on the index both from the runtime list and QSettings
        """
        if index in range(len(self.projects)):
            self.beginRemoveRows(QModelIndex(), index, index)
            project = self.projects.pop(index)
            self.endRemoveRows()

            # Re-save the settings only if this project is saved in the settings
            if project.project is not None or project.fromStartup:
                self.saveInSettings()

            # It allows the project to be deconstructed (i.e. GC'ed) very soon, not at the app shutdown time
            project.deleteLater()